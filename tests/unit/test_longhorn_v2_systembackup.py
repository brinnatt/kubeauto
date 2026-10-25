import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
import zipfile

import yaml

import importlib.util
ROOT = Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('system_gate',ROOT/'tests/helpers/longhorn-v2-systembackup.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)


import contextlib
import shlex
import os
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock,patch
class BundleContracts(unittest.TestCase):
    def test_manager_build_identity_is_exact_for_upstream_and_published_patch(self):
        upstream = '8320238242e8c804325d9588ec1db10feb9f36b8'
        for repository in ('registry.talkschool.cn:5000/brinnatt', 'hub.talkedu.cn/kubeauto',
                           'brinnatt', 'longhornio'):
            self.assertEqual(gate.manager_commit(repository+'/longhorn-manager:v1.12.1'), upstream)
            self.assertEqual(gate.manager_commit(repository+'/longhorn-manager:v1.12.1-kubeauto.1'),
                             upstream+'-kubeauto.9275735a33fe')
        for image in ('fixed-manager:v1.12.1', 'longhorn-manager:latest',
                      'longhorn-manager:v1.12.0', 'longhorn-manager:v1.12.1-kubeauto.2',
                      'longhorn-manager:v1.12.1-kubeauto.1-extra',
                      'longhorn-manager:v1.12.1-kubeauto.1@sha256:'+'a'*64):
            with self.subTest(image=image), self.assertRaisesRegex(ValueError, 'unqualified'):
                gate.manager_commit(image)

    def test_patched_bundle_checks_both_commit_fields_and_full_archive_checksum(self):
        expected, members = self.fixture()
        expected['manager_image'] = 'hub.talkedu.cn/kubeauto/longhorn-manager:v1.12.1-kubeauto.1'
        expected['manager_commit'] = gate.manager_commit(expected['manager_image'])
        metadata = members['system-probe/metadata.yaml']
        metadata.update(managerimage=expected['manager_image'], longhorngitcommit=expected['manager_commit'])
        config, data = self.objects(expected, members)
        self.assertEqual(gate.validate_bundle(config, data, expected)['zip_sha256'], hashlib.sha256(data).hexdigest())
        upstream = '8320238242e8c804325d9588ec1db10feb9f36b8'
        for wrong in (upstream, upstream+'-kubeauto.unknown', ''):
            changed = json.loads(config); changed['LonghornGitCommit'] = wrong
            with self.assertRaisesRegex(ValueError, 'configuration differs: LonghornGitCommit'):
                gate.validate_bundle(json.dumps(changed).encode(), data, expected)
            metadata['longhorngitcommit'] = wrong
            changed_config, changed_data = self.objects(expected, members)
            with self.assertRaisesRegex(ValueError, 'metadata differs: longhorngitcommit'):
                gate.validate_bundle(changed_config, changed_data, expected)
        damaged = json.loads(config); damaged['Checksum'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'Checksum'):
            gate.validate_bundle(json.dumps(damaged).encode(), data, expected)

    def test_explicit_v1_or_v2_engine_checks_real_unchanged_archive(self):
        for engine in ('v1','v2'):
            expected,members=self.fixture()
            members['system-probe/yamls/Volume.yaml']['items'][0]['spec']['dataEngine']=engine
            config,data=self.objects(expected,members)
            result=gate.validate_bundle(config,data,expected,data_engine=engine)
            self.assertEqual(hashlib.sha256(data).hexdigest(),result['zip_sha256'])
            with self.assertRaisesRegex(ValueError,'engine'):
                gate.validate_bundle(config,data,expected,data_engine='v1' if engine=='v2' else 'v2')
            if engine=='v1':
                with self.assertRaisesRegex(ValueError,'engine'):gate.validate_bundle(config,data,expected)
            else:self.assertEqual(result,gate.validate_bundle(config,data,expected))
        with self.assertRaisesRegex(ValueError,'unsupported'):gate.validate_bundle(b'',b'',{},data_engine='other')

    def fixture(self):
        expected={'name':'system-probe','manager_commit':'8320238242e8c804325d9588ec1db10feb9f36b8',
                  'target':'s3://fixture@local/backupstore/','manager_image':'fixed-manager:v1.12.1',
                  'namespace_uid':'namespace-uid','volume':'source','last_backup':'incremental',
                  'resource_uids':{('Volume','longhorn-system','source'):'volume-uid',
                                   ('PersistentVolume','','source-pv'):'pv-uid',
                                   ('PersistentVolumeClaim','business','data'):'pvc-uid',
                                   ('StorageClass','','v2'):'class-uid'}}
        metadata={'longhornversion':'v1.12.1','longhorngitcommit':expected['manager_commit'],
                  'longhornnamespaceuuid':'namespace-uid','kubernetesversion':'v1.33.6',
                  'managerimage':expected['manager_image'],'systembackupcreatedat':'2026-10-20T00:00:00Z'}
        members={'system-probe/metadata.yaml':metadata}
        for (kind,namespace,name),uid in expected['resource_uids'].items():
            row={'metadata':{'name':name,'namespace':namespace,'uid':uid}}
            if kind=='Volume':row.update(spec={'dataEngine':'v2','size':str(2*1024**3)},status={'lastBackup':'incremental'})
            members['system-probe/yamls/'+kind+'.yaml']={'apiVersion':'v1','kind':kind+'List','items':[row]}
        return expected,members

    def objects(self,expected,members,duplicate=False):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            for name,value in members.items():z.writestr(name,yaml.safe_dump(value))
            if duplicate:z.writestr(next(iter(members)),yaml.safe_dump(next(iter(members.values()))))
        raw=data.getvalue()
        config={'Name':expected['name'],'LonghornVersion':'v1.12.1','LonghornGitCommit':expected['manager_commit'],
                'BackupTargetURL':expected['target'],'ManagerImage':expected['manager_image'],
                'CreatedAt':'2026-10-20T00:00:00Z','Checksum':hashlib.sha256(raw).hexdigest()}
        return json.dumps(config).encode(),raw

    def test_original_resource_ids_and_whole_zip_sha256_without_extracting_or_leaking(self):
        expected,members=self.fixture();config,data=self.objects(expected,members)
        result=gate.validate_bundle(config,data,expected)
        self.assertEqual(result['original_resources_verified'],4)
        self.assertEqual(result['zip_sha256'],hashlib.sha256(data).hexdigest())
        self.assertFalse(result['system_restore_proven'])
        self.assertFalse(result['new_volume_data_backup_proven'])
        for secret in ('namespace-uid','volume-uid',expected['target'],expected['manager_image']):
            self.assertNotIn(secret,json.dumps(result))

    def test_remote_config_drift_or_checksum_mismatch_cannot_pass(self):
        expected,members=self.fixture();config,data=self.objects(expected,members)
        original=json.loads(config)
        for field in ('Name','LonghornVersion','LonghornGitCommit','BackupTargetURL','ManagerImage','CreatedAt','Checksum'):
            with self.subTest(field=field):
                changed=dict(original);changed[field]=''
                with self.assertRaisesRegex(ValueError, field) as error:
                    gate.validate_bundle(json.dumps(changed).encode(),data,expected)
                self.assertNotIn(expected['target'], str(error.exception))

    def test_metadata_uses_yaml_v2_lowercase_go_names_not_json_tags(self):
        expected,members=self.fixture()
        # Fixed official systemBackupMeta has only json tags. yaml.v2's
        # getStructInfo ignores those tags and lowercases exported Go names.
        original=members['system-probe/metadata.yaml']
        aliases={'longhornversion':'longhornVersion','longhorngitcommit':'longhornGitCommit',
                 'longhornnamespaceuuid':'longhornNamspaceUUID','kubernetesversion':'kubernetesVersion',
                 'managerimage':'managerImage','systembackupcreatedat':'systemBackupCreatedAt'}
        for key,alias in aliases.items():
            changed=copy.deepcopy(members);value=changed['system-probe/metadata.yaml'].pop(key)
            changed['system-probe/metadata.yaml'][alias]=value
            config,data=self.objects(expected,changed)
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,key):
                gate.validate_bundle(config,data,expected)
        self.assertEqual(set(original),set(aliases))

    def test_sha512_and_truncated_block_checksum_are_not_file_checksums(self):
        expected,members=self.fixture();config,data=self.objects(expected,members)
        for wrong in (hashlib.sha512(data).hexdigest(), hashlib.sha512(data).hexdigest()[:64]):
            changed=json.loads(config);changed['Checksum']=wrong
            with self.assertRaisesRegex(ValueError, 'Checksum'):
                gate.validate_bundle(json.dumps(changed).encode(),data,expected)

    def test_metadata_or_original_source_uid_last_backup_and_engine_drift_cannot_pass(self):
        for defect in ('metadata','namespace','kubernetes','git','uid','missing','last-backup','engine','capacity'):
            with self.subTest(defect=defect):
                expected,members=self.fixture();metadata=members['system-probe/metadata.yaml']
                volume=members['system-probe/yamls/Volume.yaml']['items'][0]
                if defect=='metadata':del members['system-probe/metadata.yaml']
                if defect=='namespace':metadata['longhornnamespaceuuid']='foreign'
                if defect=='kubernetes':metadata['kubernetesversion']='v1.32.0'
                if defect=='git':metadata['longhorngitcommit']='other'
                if defect=='uid':volume['metadata']['uid']='replaced'
                if defect=='missing':del members['system-probe/yamls/PersistentVolume.yaml']
                if defect=='last-backup':volume['status']['lastBackup']='old'
                if defect=='engine':volume['spec']['dataEngine']='v1'
                if defect=='capacity':volume['spec']['size']='1'
                config,data=self.objects(expected,members)
                with self.assertRaises(ValueError):gate.validate_bundle(config,data,expected)

    def test_unsafe_zip_member_duplicate_and_excluded_kinds_cannot_pass(self):
        expected,original=self.fixture()
        for name,kind in (('../escape.yaml','VolumeList'),('/absolute.yaml','VolumeList'),
                          ('system-probe/../escape.yaml','VolumeList'),('other/metadata.yaml','VolumeList'),
                          ('system-probe/yamls/secrets.yaml','SecretList'),
                          ('system-probe/yamls/nodes.yaml','NodeList'),('system-probe/yamls/pods.yaml','PodList')):
            with self.subTest(name=name):
                members=copy.deepcopy(original);members[name]={'apiVersion':'v1','kind':kind,'items':[]}
                config,data=self.objects(expected,members)
                with self.assertRaises(ValueError):gate.validate_bundle(config,data,expected)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            config,data=self.objects(expected,original,duplicate=True)
        with self.assertRaises(ValueError):gate.validate_bundle(config,data,expected)

    def test_actual_zip_crc_failure_cannot_be_hidden_by_matching_whole_zip_sha256(self):
        expected,members=self.fixture();config,data=self.objects(expected,members)
        changed=bytearray(data)
        # Change content in the first uncompressed member, retaining its stored
        # CRC. Recompute outer SHA256 so validation must reach ZipFile.read.
        offset=data.index(b'kubernetesversion')
        changed[offset]=ord('x');raw=bytes(changed)
        cfg=json.loads(config);cfg['Checksum']=hashlib.sha256(raw).hexdigest()
        with self.assertRaises(zipfile.BadZipFile):gate.validate_bundle(json.dumps(cfg).encode(),raw,expected)



class LifecycleContracts(unittest.TestCase):
    def fixture(self,directory):
        state={'run_id':'run','cluster_uid':'cluster','namespace_uid':'namespace'}
        target={'metadata':{'uid':'target'},'spec':{'backupTargetURL':'s3://bucket@local/','credentialSecret':'secret'}}
        ns={'metadata':{'uid':'namespace'}}
        row={'product_binding':{'namespace_uid':'namespace','target_uid':'target','secret_name':'secret',
                               'fixture_spec':copy.deepcopy(target['spec'])},'name':'store'}
        store=SimpleNamespace(receipt=Mock(return_value=row),inspect=Mock(return_value={'Id':'container','State':{'Running':True}}),
                              directory=Path(directory)/'store',mc=Mock(),cleanup=Mock(),prepare=Mock(),qualify=Mock(),product_configuration=Mock(return_value={'configured':True}))
        store.directory.mkdir()
        lab=SimpleNamespace(DIRECTORY=Path(directory),owner=Mock(return_value=state),backup_store=lambda:store,
                            get=Mock(side_effect=lambda kind,*args,**kwargs: target if kind=='backuptargets.longhorn.io' else ns),
                            execute=Mock(),write_private=Mock(),remove=Mock(),wait=Mock(),load_helper=Mock())
        return gate.Probe(lab),lab,store,target

    def test_target_container_namespace_or_journal_drift_refuses_before_delete(self):
        for defect in ('target','secret','poll','namespace','container','journal','request'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                probe,lab,store,target=self.fixture(directory)
                row=dict(probe.identity(),name='system-probe-'+'a'*24)
                row['request']={'kind':'system-backup','name':row['name'],'volume_backup_policy':'disabled'}
                if defect=='target':target['metadata']['uid']='changed'
                if defect=='secret':target['spec']['credentialSecret']='changed'
                if defect=='poll':target['spec']['pollInterval']='unexpected'
                if defect=='namespace':store.receipt.return_value['product_binding']['namespace_uid']='changed'
                if defect=='container':store.inspect.return_value=None
                if defect=='journal':row['run_id']='foreign'
                if defect=='request':row['request']['volume_backup_policy']='always'
                probe.record.write_text(json.dumps(row))
                with self.assertRaises(ValueError):probe.journal()
                lab.remove.assert_not_called()
                lab.execute.assert_not_called()

    def test_unknown_uid_replacement_restore_consumer_and_foreign_objects_preserve_native_and_journal(self):
        for defect in ('unknown','replaced','consumer','object'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                probe,lab,store,_=self.fixture(directory)
                row={'name':'system-probe-'+'a'*24,'uid':'original','request':{}}
                probe.record.write_text('owned')
                obj={'metadata':{'uid':'original'}}
                if defect=='unknown':row.pop('uid')
                if defect=='replaced':obj['metadata']['uid']='replaced'
                def get(kind,*args,**kwargs):
                    if kind=='systemrestores.longhorn.io':return {'items':[{'spec':{'systemBackup':row['name']}}] if defect=='consumer' else []}
                    return obj
                lab.get.side_effect=get
                keys=[probe.prefix(row)+'system-backup.cfg']
                if defect=='object':keys.append(probe.prefix(row)+'foreign')
                with patch.object(probe,'journal',return_value=row),patch.object(probe,'object_keys',return_value=keys),patch.object(probe,'cleanup_readback'):
                    with self.assertRaises(ValueError):probe.cleanup()
                lab.remove.assert_not_called()
                self.assertTrue(probe.record.exists())
                store.cleanup.assert_not_called()

    def test_scoped_cleanup_deletes_original_before_waiting_for_both_remote_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            probe,lab,store,_=self.fixture(directory)
            row={'name':'system-probe-'+'a'*24,'uid':'original','request':{}}
            probe.record.write_text('owned')
            obj={'metadata':{'uid':'original'}}
            deleted={'value':False};calls=[]
            lab.get.side_effect=lambda kind,*args,**kwargs: {'items':[]} if kind=='systemrestores.longhorn.io' else (None if deleted['value'] else obj)
            def remove(value):
                self.assertIs(value,obj);deleted['value']=True;calls.append('delete-native')
            lab.remove.side_effect=remove
            def keys(value):
                calls.append('list-remote')
                return [] if deleted['value'] else [probe.prefix(row)+'system-backup.cfg',probe.prefix(row)+'system-backup.zip']
            lab.wait.side_effect=lambda stage,timeout,check:self.assertTrue(check())
            with patch.object(probe,'journal',return_value=row),patch.object(probe,'object_keys',side_effect=keys),patch.object(probe,'cleanup_readback'),contextlib.redirect_stdout(io.StringIO()) as output:
                probe.cleanup()
                probe.cleanup()
            self.assertEqual(calls,['list-remote','delete-native','list-remote'])
            self.assertEqual(lab.remove.call_count,1)
            self.assertFalse(probe.record.exists())
            self.assertEqual(output.getvalue().splitlines(),['LONGHORN_V2_SYSTEMBACKUP_FIXTURES_CLEAN_PASS'])

    def test_readback_rejects_missing_or_extra_objects_before_downloading_and_never_extracts(self):
        with tempfile.TemporaryDirectory() as directory:
            probe,lab,store,_=self.fixture(directory)
            row={'name':'system-probe-'+'a'*24}
            for keys in ([],[probe.prefix(row)+'system-backup.cfg'],[probe.prefix(row)+'foreign']):
                with patch.object(probe,'object_keys',return_value=keys),self.assertRaises(ValueError):probe.readback(row,{})
            store.mc.assert_not_called()
            lab.execute.assert_not_called()
            self.assertNotIn('extractall',Path(gate.__file__).read_text())

    def test_private_actual_zip_copy_validates_bytes_and_always_removes_both_temporary_files(self):
        for failure in ('none', 'config-changed', 'metadata'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                probe,lab,store,_=self.fixture(directory)
                expected,members=BundleContracts().fixture()
                config,archive=BundleContracts().objects(expected,members)
                row={'name':expected['name'],'store_name':'store','container_id':'container'}
                reads={'count':0}
                def mc(*args):
                    if args[0]=='cat':
                        reads['count']+=1
                        return config.decode() if reads['count']==1 or failure!='config-changed' else '{}'
                    self.assertEqual(args[0],'cp')
                    self.assertTrue(probe.record.exists())
                    return ''
                def execute(args):
                    if args[1]=='cp':
                        self.assertTrue(probe.record.exists())
                        Path(args[-1]).write_bytes(archive)
                    return SimpleNamespace(stdout=str(len(archive)) if 'stat' in args else '')
                store.mc.side_effect=mc
                lab.execute.side_effect=execute
                lab.write_private.side_effect=lambda path,value:path.write_text(json.dumps(value))
                keys=[probe.prefix(row)+name for name in ('system-backup.cfg','system-backup.zip')]
                if failure=='metadata':expected['namespace_uid']='changed'
                mkdtemp=tempfile.mkdtemp
                with patch.object(probe,'object_keys',return_value=sorted(keys)), \
                     patch.object(tempfile,'mkdtemp',side_effect=lambda **kw:mkdtemp(prefix=kw['prefix'],dir=directory)), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    if failure=='none':
                        result=probe.readback(row,expected)
                        self.assertEqual(result['zip_sha256'],hashlib.sha256(archive).hexdigest())
                    else:
                        with self.assertRaises(ValueError):probe.readback(row,expected)
                failures=list(Path(directory).glob('longhorn-system-bundle-failure-*'))
                self.assertEqual(len(failures),int(failure!='none'))
                for saved in failures:
                    self.assertEqual(saved.stat().st_mode & 0o777,0o700)
                    self.assertEqual((saved/'config.json').read_bytes(),config)
                    self.assertEqual((saved/'bundle.zip').read_bytes(),archive)
                    self.assertEqual((saved/'config.json').stat().st_mode & 0o777,0o600)
                    self.assertEqual((saved/'bundle.zip').stat().st_mode & 0o777,0o600)
                    self.assertIn('PRIVATE_BUNDLE directory='+str(saved),output.getvalue())
                self.assertNotIn(expected['target'],output.getvalue())
                self.assertFalse((store.directory/'system-bundle-readback').exists())
                self.assertEqual(lab.execute.call_args.args[0],['docker','exec','container','rm','-f','--',row['container_temporary']])

    def test_business_preserves_results_and_restore_hooks_cleanup_failure_retains_store(self):
        for failure in ('none','backup','system','cleanup'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                probe,lab,store,_=self.fixture(directory);calls=[]
                def old_verify(*args,**kwargs):
                    calls.append('backup')
                    if failure=='backup':raise ValueError('backup failure')
                    return ('post-data','post-hash')
                def old_cleanup(*args,**kwargs):calls.append('business-clean')
                def native(*args):
                    calls.append('system')
                    if failure=='system':raise ValueError('native failure')
                def system_cleanup():
                    calls.append('system-clean')
                    if failure=='cleanup':raise ValueError('uncertain ownership')
                @contextlib.contextmanager
                def scope(cleanup):
                    try:yield
                    finally:cleanup()
                lab.scoped_cleanup=scope;lab.verify_backup=old_verify;lab.cleanup_business=old_cleanup
                def business(mode,pid,**kwargs):
                    self.assertEqual((mode,pid),('backup',12))
                    self.assertEqual(lab.verify_backup({},None,pid,'data','hash',{}),('post-data','post-hash'))
                lab.business=business
                fake=SimpleNamespace(verify=native,cleanup=system_cleanup)
                with patch.object(gate,'Probe',return_value=fake):
                    if failure=='none':gate.business(lab,12)
                    else:
                        with self.assertRaises(ValueError):gate.business(lab,12)
                self.assertIs(lab.verify_backup,old_verify)
                self.assertIs(lab.cleanup_business,old_cleanup)
                if failure=='cleanup':
                    store.cleanup.assert_not_called()
                    self.assertNotIn('business-clean',calls)
                else:
                    self.assertLess(calls.index('system-clean'),calls.index('business-clean'))
                    store.cleanup.assert_called_once()
                if failure=='backup':self.assertNotIn('system',calls)




class ShellContracts(unittest.TestCase):
    def observation(self):
        source='a'*64
        result={'name':'system-probe-'+'a'*24,'uid':'system-uid','volume_uid':'source-uid','last_backup':'incremental',
                'config_size':120,'zip_size':900,'members':8,'resource_count':10,'original_resources_verified':4,
                'attached_restore_refused':True,'volume_backup_count_unchanged':True,'secret_pod_node_excluded':True,
                'system_restore_proven':False,'new_volume_data_backup_proven':False,'volume_backup_policy':'disabled',
                'zip_checksum_algorithm':'sha256','zip_sha256':'c'*64,'config_sha256':'d'*64}
        log='LONGHORN_V2_SYSTEMBACKUP_OBSERVED '+json.dumps(result)+'\n'
        for mode,digest in (('full','e'*64),('incremental','f'*64)):
            receipt={'backup':mode,'volume':'source','size':2*1024**3,'incremental':mode=='incremental',
                     'objects':{'object':{'size':5,'sha256':'a'*64}}}
            log+='LONGHORN_BACKUP_OBJECT_RECEIPT mode='+mode+' '+json.dumps(receipt)+'\n'
            log+='LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode='+mode+' backup_sha256='+digest+' source_sha256='+source+' independent_volume=restore-'+mode+'\n'
        log+='LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+source+' replica_nodes=node1,node2,node3\n'
        for marker in ('LONGHORN_V2_SYSTEMBACKUP_FIXTURES_CLEAN_PASS','LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED',
                       'LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact',
                       'LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED','LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS',
                       'LONGHORN_BACKUP_STORE_CLEAN_PASS','LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS','LONGHORN_BACKUP_BUSINESS_PASS'):
            log+=marker+'\n'
        return log

    def test_result_verifier_rejects_partial_bundle_data_refusal_or_cleanup(self):
        log=self.observation()
        gate.verify_log(log)
        for prefix in ('LONGHORN_V2_SYSTEMBACKUP_OBSERVED','LONGHORN_BACKUP_OBJECT_RECEIPT mode=full',
                       'LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=incremental',
                       'LONGHORN_FSYNC_REMOUNT_VERIFIED','LONGHORN_V2_SYSTEMBACKUP_FIXTURES_CLEAN_PASS',
                       'LONGHORN_BACKUP_STORE_CLEAN_PASS','LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED'):
            with self.subTest(prefix=prefix),self.assertRaises(ValueError):
                gate.verify_log('\n'.join(line for line in log.splitlines() if not line.startswith(prefix))+'\n')
        with self.assertRaises(ValueError):gate.verify_log(log.replace('"system_restore_proven": false','"system_restore_proven": true'))

    def test_real_shell_stops_on_failed_prerequisites_result_or_cleanup(self):
        script=Path(gate.__file__).with_suffix('.sh').read_text()
        cases=[(mode,failure) for mode in ('v2-systembackup-qualification','v2-systembackup-recovery','v2-systembackup-cleanup')
               for failure in ('none','transport','qualification','baseline','business','result','cleanup','final-baseline','core')
               if not (mode!='v2-systembackup-qualification' and failure=='qualification')
               and not (mode=='v2-systembackup-cleanup' and failure in ('business','result'))]
        for mode,failure in cases:
            with self.subTest(mode=mode,failure=failure),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);(base/'.venv/bin').mkdir(parents=True);(base/'tests/helpers').mkdir(parents=True);(base/'kube-bin').mkdir()
                python=base/'.venv/bin/python';python.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' \"$@\"\n');python.chmod(0o755)
                kubectl=base/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
                observation=self.observation()
                if mode=='v2-systembackup-recovery':
                    result={'uid':'original','source_bytes':123,'source_sha256':'a'*64,'zip_sha256':'b'*64,'zip_checksum_algorithm':'sha256',
                            'original_create_replayed':True,'qualified_system_restore':False}
                    observation='LONGHORN_V2_SYSTEMBACKUP_RECOVERY_OBSERVED '+json.dumps(result)+'\n'
                    observation+='\n'.join(('LONGHORN_V2_SYSTEMBACKUP_FIXTURES_CLEAN_PASS','LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                                            'LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS','LONGHORN_BACKUP_STORE_CLEAN_PASS',
                                            'LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED'))+'\n'
                if failure=='result':observation=observation.replace('LONGHORN_V2_SYSTEMBACKUP_FIXTURES_CLEAN_PASS','')
                (base/'observation.log').write_text(observation)
                helper='''import os,signal,sys,time
from pathlib import Path
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if (mode=='verify-transport' and failure=='transport') or (mode=='qualification-current' and failure=='qualification') or (mode=='verify-core' and failure=='core'):sys.exit(17)
'''
                for name in ('core','install','backups'):(base/f'tests/helpers/longhorn-v2-{name}.py').write_text(helper)
                product='''import os,runpy,sys
from pathlib import Path
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
with open('calls.txt','a') as stream:stream.write('system-'+mode+'\\n')
if mode in ('baseline','recovery-baseline','cleanup-baseline'):
 p=Path('baselines');n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))
 if failure=='baseline' or failure=='final-baseline' and n>1:sys.exit(17)
 print('LONGHORN_V2_BACKUP_BASELINE_VERIFIED target_sha256=stable backup_objects=0 fixture_objects=0')
if mode=='cleanup' and failure=='cleanup':sys.exit(17)
if mode in ('business','recover'):
 if failure=='business':sys.exit(17)
 print(Path('observation.log').read_text())
if mode in ('verify','verify-recovery'):runpy.run_path(os.environ['SYSTEM_SOURCE'],run_name='__main__')
'''
                (base/'tests/helpers/longhorn-v2-systembackup.py').write_text(product)
                source=script.replace('BASE=/usr/local/kubeauto','BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-systembackup.',str(base/'work.'))
                result=subprocess.run(['bash','-c',source,'contract',mode],
                                      env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',FAIL_MODE=failure,SYSTEM_SOURCE=gate.__file__),
                                      capture_output=True,text=True,timeout=15)
                for name in re.findall(r'private_diagnostic=(/var/tmp/longhorn-v2-systembackup-failure-\S+)',result.stderr):Path(name).unlink()
                self.assertEqual(result.returncode==0,failure=='none',result.stdout+result.stderr)
                self.assertEqual(('LONGHORN_'+mode.upper().replace('-','_')+'_PASS') in result.stdout,failure=='none')
                calls=(base/'calls.txt').read_text().splitlines()
                if failure in ('transport','qualification','baseline'):
                    self.assertNotIn('system-business',calls);self.assertNotIn('system-recover',calls);self.assertNotIn('system-cleanup',calls)
                else:
                    if mode=='v2-systembackup-cleanup':
                        self.assertNotIn('system-business',calls);self.assertNotIn('system-recover',calls)
                        self.assertNotIn('system-verify',calls)
                    else:
                        self.assertIn('system-business' if mode=='v2-systembackup-qualification' else 'system-recover',calls)
                    self.assertLess(calls.index('system-cleanup'),calls.index('relay-clean'))
                    self.assertEqual(calls[-1],'verify-core')


class ProductWorkflowContracts(unittest.TestCase):
    def test_checksum_failure_cleanup_requires_exact_archive_and_no_consumers(self):
        for failure in ('none','journal','symlink','identity','system-journal','system','restore','backup','remote','status'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base=Path(directory);(base/'tests/evidence').mkdir(parents=True)
                journal={'volume':'source','volume_uid':'original','namespace':'business'}
                identity={'run_id':'run','cluster_uid':'cluster','namespace_uid':'ns',
                          'target_uid':'target','store_name':'store','container_id':'container'}
                original={'recovery_journal':dict(identity,name='system-probe-original'),
                          'original_backup_baseline':'original-empty-baseline'}
                evidence={'status':'failed','rc':1,'owned_state':{'source_journal':journal,'cluster_uid':'cluster','namespace_uid':'ns'}}
                if failure=='status':evidence['status']='pass'
                for name,value in (('null-conditions',original),('checksum',evidence)):
                    (base/('tests/evidence/longhorn-v2-systembackup-'+name+'-first-failure-20261020.json')).write_text(json.dumps(value))
                record=base/'longhorn-install-business.json'
                record.write_text(json.dumps(dict(journal,volume_uid='foreign') if failure=='journal' else journal))
                if failure=='symlink':record.rename(base/'original');record.symlink_to(base/'original')
                system_record=base/'longhorn-systembackup-qualification.json'
                if failure=='system-journal':system_record.write_text('{}')
                if failure=='identity':identity['container_id']='replaced'
                probe=SimpleNamespace(identity=Mock(return_value=identity),record=system_record,
                                      object_keys=Mock(return_value=['foreign'] if failure=='remote' else []))
                def get(kind,**kwargs):
                    return {'items':[{}] if {'systembackups.longhorn.io':'system','systemrestores.longhorn.io':'restore','backups.longhorn.io':'backup'}[kind]==failure else []}
                lab=SimpleNamespace(DIRECTORY=base,get=Mock(side_effect=get))
                with patch.object(gate,'BASE',base), patch.object(gate,'Probe',return_value=probe):
                    if failure=='none':self.assertEqual(gate.cleanup_state(lab),'original-empty-baseline')
                    else:
                        with self.assertRaises(ValueError):gate.cleanup_state(lab)
                self.assertTrue(record.exists())

    def test_private_api_diagnostic_retains_stderr_and_cause_without_public_output(self):
        error=RuntimeError('public-safe error');error.stderr='sensitive native API conflict';error.returncode=1
        outer=ValueError('cleanup failed');outer.__cause__=error
        with contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()) as err:
            path=Path(gate.write_failure_diagnostic(outer))
        try:
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertIn(error.stderr,path.read_text());self.assertIn('returncode: 1',path.read_text())
            self.assertEqual(out.getvalue()+err.getvalue(),'')
        finally:path.unlink()

    def test_archived_unconfirmed_create_recovery_uses_original_uid_and_product_replay(self):
        for failure in ('none', 'journal', 'native', 'owner', 'source', 'consumer', 'credential', 'credential-data', 'replay', 'bytes', 'bundle'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base=Path(directory);(base/'tests/evidence').mkdir(parents=True)
                row={'name':'system-probe-'+'a'*24,'run_id':'run','namespace_uid':'namespace',
                     'request':{'kind':'system-backup','name':'system-probe-'+'a'*24,'volume_backup_policy':'disabled'}}
                original=copy.deepcopy(row)
                obj={'metadata':{'uid':'original','creationTimestamp':'time','ownerReferences':[{'uid':'target'}]},
                     'spec':{'volumeBackupPolicy':'disabled'},'status':{'state':'Ready','conditions':None,'managerImage':'fixed'}}
                saved=copy.deepcopy(obj)
                journal={'namespace':'business','namespace_uid':'business-uid','volume':'volume','volume_uid':'volume-uid',
                         'pv':'pv','pv_uid':'pv-uid','storage_class':'class',
                         'objects':{'PersistentVolumeClaim/data':'claim','Pod/writer':'writer'},
                         'backups':{'incremental':{'request':{'mode':'incremental'}}}}
                identity={key:journal[key] for key in ('namespace','namespace_uid','volume','volume_uid','pv','pv_uid')}
                digest=hashlib.sha256(b'run-after-full-after-incremental'*262144).hexdigest()
                evidence={'status':'failed','classification':'product','rc':1,'recovery_journal':original,
                          'native':saved,'source_identity':identity,'source_sha256':digest}
                (base/'tests/evidence/longhorn-v2-systembackup-null-conditions-first-failure-20261020.json').write_text(json.dumps(evidence))
                if failure=='journal':row['uid']='unproven'
                if failure=='native':obj['metadata']['uid']='foreign'
                if failure=='owner':obj['metadata']['ownerReferences']=[{'uid':'foreign'}]
                if failure=='source':journal['volume_uid']='foreign'
                (base/'longhorn-install-business.json').write_text(json.dumps(journal))
                calls=[];reads={'native':0}
                def get(kind,*args,**kwargs):
                    if kind=='systembackups.longhorn.io':
                        reads['native']+=1
                        if failure=='replay' and reads['native']>1:return dict(obj,metadata=dict(obj['metadata'],uid='replaced'))
                        return obj
                    if kind=='systemrestores.longhorn.io':return {'items':[{}] if failure=='consumer' else []}
                    if kind=='storageclass':return {'metadata':{'uid':'class','name':'class'}}
                    if kind=='pod':return {'metadata':{'uid':'writer'}}
                    if kind=='secret':return {'metadata':{'uid':'replaced' if failure=='credential' else 'secret-uid'},
                                              'data':{'fixture':'changed' if failure=='credential-data' else 'original'}}
                    raise AssertionError(kind)
                probe=SimpleNamespace(journal=lambda:row,record=base/'journal',
                    store=SimpleNamespace(product_configuration=Mock(side_effect=ValueError('previous backup product binding needs recovery')),
                                          receipt=lambda:{'product_binding':{'fixture_spec':{'backupTargetURL':'target'},
                                                    'secret_name':'secret','secret_uid':'secret-uid',
                                                    'secret_digest':hashlib.sha256(json.dumps({'fixture':'original'},sort_keys=True).encode()).hexdigest()}},
                                          cleanup=Mock(side_effect=lambda:calls.append('store-clean'))),
                    readback=Mock(side_effect=ValueError('bundle') if failure=='bundle' else lambda *args:{'zip_sha256':'a'*64,'zip_checksum_algorithm':'sha256'}),
                    cleanup=Mock(side_effect=lambda:calls.append('system-clean')))
                lab=SimpleNamespace(DIRECTORY=base,get=Mock(side_effect=get),
                    load_helper=lambda name:SimpleNamespace(check_request=Mock(),request_object=lambda r:r),
                    run_requests=Mock(side_effect=lambda *args,**kwargs:calls.append('product-replay')),
                    write_private=Mock(),replica_topology=Mock(return_value=True),payload_program=Mock(return_value='read-only'),
                    kubectl=Mock(return_value=SimpleNamespace(stdout='wrong' if failure=='bytes' else digest)),
                    cleanup_business=Mock(side_effect=lambda:calls.append('business-clean')))
                with patch.object(gate,'BASE',base),patch.object(gate,'Probe',return_value=probe),contextlib.redirect_stdout(io.StringIO()) as output:
                    if failure=='none':gate.recover(lab,12)
                    else:
                        with self.assertRaises(ValueError):gate.recover(lab,12)
                if failure=='none':
                    self.assertEqual(calls,['product-replay','system-clean','business-clean','store-clean'])
                    self.assertEqual(row['uid'],'original')
                    lab.run_requests.assert_called_once_with(12,[original['request']],backup_configuration={'longhorn_backup_target':'target','longhorn_backup_secret':'secret'})
                    self.assertIn('LONGHORN_V2_SYSTEMBACKUP_RECOVERY_OBSERVED',output.getvalue())
                else:
                    probe.cleanup.assert_not_called();lab.cleanup_business.assert_not_called();probe.store.cleanup.assert_not_called()
                    self.assertNotIn('LONGHORN_V2_SYSTEMBACKUP_RECOVERY_OBSERVED',output.getvalue())
                    if failure not in ('replay','bytes','bundle'):lab.run_requests.assert_not_called()
                probe.store.product_configuration.assert_not_called()

    def test_bundle_and_refusal_use_config_kubecli_original_ids_and_no_extra_data_backup(self):
        for failure in ('none', 'foreign', 'source', 'backup-watermark', 'pv', 'readback'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                record = {'volume':'source','volume_uid':'volume-uid','pv':'source-pv','pv_uid':'pv-uid',
                          'namespace':'business','objects':{'PersistentVolumeClaim/data':'pvc-uid'},'storage_class':'v2',
                          'backups':{mode:{'request':{'mode':mode}} for mode in ('full','incremental')}}
                volume = {'metadata':{'uid':'volume-uid'},'spec':{'dataEngine':'v2'},'status':{'lastBackup':'incremental'}}
                if failure=='source':volume['metadata']['uid']='foreign'
                if failure=='backup-watermark':volume['status']['lastBackup']='old'
                calls=[]
                native={'metadata':{'uid':'system-uid'},'spec':{'volumeBackupPolicy':'disabled'},
                        'status':{'state':'Ready','version':'v1.12.1','managerImage':'registry.talkschool.cn:5000/brinnatt/longhorn-manager:v1.12.1-kubeauto.1'}}
                def get(kind,name=None,namespace=None):
                    if kind=='systembackups.longhorn.io':return native if name else {'items':[native] if failure=='foreign' else []}
                    if kind=='systemrestores.longhorn.io':return {'items':[]}
                    if kind=='volumes.longhorn.io':return volume
                    if kind=='backups.longhorn.io':return {'items':[{'metadata':{'uid':mode}} for mode in ('full','incremental')]}
                    if kind=='pv':return {'metadata':{'uid':'changed' if failure=='pv' else 'pv-uid'}}
                    if kind=='pvc':return {'metadata':{'uid':'pvc-uid'}}
                    if kind=='storageclass':return {'metadata':{'uid':'class-uid','name':'v2'}}
                    raise AssertionError(kind)
                def wait(stage,timeout,check):
                    value=check()
                    if not value:raise TimeoutError(stage)
                    return value
                lab=SimpleNamespace(DIRECTORY=Path(directory),backup_store=Mock(),get=Mock(side_effect=get),
                    wait=Mock(side_effect=wait),replica_topology=Mock(return_value=True),healthy_replicas=Mock(return_value=True),
                    write_private=Mock(),load_helper=Mock(return_value=SimpleNamespace(check_request=Mock(),request_object=lambda x:x)),
                    run_requests=Mock(side_effect=lambda *args,**kwargs:calls.append((args,kwargs))))
                probe=gate.Probe(lab)
                expected_identity={'run_id':'run','cluster_uid':'cluster','namespace_uid':'namespace','target_uid':'target','store_name':'store','container_id':'container'}
                def readback(row,expected):
                    self.assertEqual(row['uid'],'system-uid')
                    self.assertEqual(expected['manager_commit'],
                                     '8320238242e8c804325d9588ec1db10feb9f36b8-kubeauto.9275735a33fe')
                    self.assertEqual(expected['last_backup'],'incremental')
                    self.assertEqual(expected['resource_uids'][('Volume','longhorn-system','source')],'volume-uid')
                    self.assertEqual(expected['resource_uids'][('PersistentVolumeClaim','business','data')],'pvc-uid')
                    if failure=='readback':raise ValueError('bundle failed')
                    return {'system_restore_proven':False}
                configuration={'longhorn_backup_target':'s3://store@local/','longhorn_backup_secret':'secret'}
                with patch.object(probe,'identity',return_value=expected_identity),patch.object(probe,'readback',side_effect=readback),patch.object(probe,'bound_object',return_value=native),patch.object(probe,'cleanup') as cleanup,contextlib.redirect_stdout(io.StringIO()) as output:
                    if failure=='none':probe.verify(record,12,configuration)
                    else:
                        with self.assertRaises((ValueError,TimeoutError)):probe.verify(record,12,configuration)
                    if failure=='none':
                        self.assertEqual(len(calls),2)
                        self.assertEqual(calls[0][0][0],12)
                        self.assertEqual(calls[0][0][1][0]['volume_backup_policy'],'disabled')
                        self.assertEqual(calls[1][0][1][0]['kind'],'system-restore')
                        self.assertEqual(calls[1][1]['expected_refusal'],'system restore requires all volumes detached')
                        self.assertEqual(calls[1][1]['backup_configuration'],configuration)
                        cleanup.assert_called_once()
                        self.assertIn('LONGHORN_V2_SYSTEMBACKUP_OBSERVED',output.getvalue())
                    else:
                        cleanup.assert_not_called()
                        self.assertNotIn('LONGHORN_V2_SYSTEMBACKUP_OBSERVED',output.getvalue())
                        self.assertEqual(lab.run_requests.call_count,1 if failure=='readback' else 0)
                        if failure=='readback':self.assertEqual(lab.write_private.call_args.args[1]['uid'],'system-uid')

    def test_fixed_dispatch_allowlists_live_route_and_matrix_owner(self):
        mode='v2-systembackup-qualification'
        command='source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-'+mode+'\n'
        result=subprocess.run(['bash','-c',command,'contract',str(ROOT/'tests/helpers/longhorn-runner.sh')],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout,mode+' LONGHORN_V2_SYSTEMBACKUP_QUALIFICATION_PASS')
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        for variable in ('module','mode'):
            branches=re.findall(r'case "\$'+variable+r'" in ([^\n]+)',runner)
            self.assertEqual(len(branches),1)
            self.assertIn(mode,branches[0].split(')')[0].split('|'))
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertIn('  '+mode+'|v2-systembackup-recovery|v2-systembackup-cleanup)\n    exec bash "$BASE/tests/helpers/longhorn-v2-systembackup.sh" "$MODE"',live)
        cases=yaml.safe_load((ROOT/'tests/longhorn-test-matrix.yaml').read_text())['cases']
        self.assertEqual([row['id'] for row in cases if 'systembackup_qualification' in row],['LONGHORN-48'])
