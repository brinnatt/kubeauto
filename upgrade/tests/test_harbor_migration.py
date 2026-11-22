"""Real stopped archives and private checkpoints; no Docker daemon or lab."""
from copy import deepcopy
import json
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import subprocess
import sys
import signal
import unittest
from unittest.mock import patch

from upgrade.product.library import harbor_migration as migration


class Fixture(migration.Harbor):
    def __init__(self, root, **kwargs):
        self.trace=[];self.fault=None;self.version='v2.13.0';self.prepares={}
        data=root/'data';data.mkdir();(data/'harbor').mkdir()
        (data/'harbor/harbor.yml').write_text('private original config\n')
        (data/'harbor/docker-compose.yml').write_text('private original Compose\n')
        for directory in ['database/pg15','registry','secret/keys','redis']:
            (data/directory).mkdir(parents=True)
        (data/'database/pg15/PG_VERSION').write_text('15\n')
        (data/'redis/dump.rdb').write_bytes(b'REDIS0011 fixture persisted queue')
        (data/'registry/confirmed-blob').write_bytes(b'original confirmed image bytes')
        (data/'secret/keys/secretkey').write_text('private-original-encryption-key')
        (root/'bin').mkdir();tls=root/'tls';tls.mkdir()
        for name in ['certificate','key','ca']:(tls/name).write_text('private-'+name)
        config={'hostname':'registry.example.invalid','https':{'port':8443,
                'certificate':str(tls/'certificate'),'private_key':str(tls/'key')},'data_volume':str(data)}
        super().__init__(data_directory=str(data),backup_directory=str(root/'backup'),
            binary_directory=str(root/'bin'),configuration=config,ca_file=str(tls/'ca'),**kwargs)
        self.layout={'name':'harbor','services':{name:{'image':'goharbor/'+name+':v2.13.0',
            'container_name':name,'volumes':[]} for name in ['core','proxy','registry','postgresql','redis','log','jobservice']}}
        for name,target,source in [('core','/data',data),('registry','/storage',data/'registry'),
                                   ('postgresql','/var/lib/postgresql/data',data/'database'),
                                   ('redis','/var/lib/redis',data/'redis')]:
            self.layout['services'][name]['volumes']=[{'type':'bind','source':str(source),'target':target}]
        (self.install/'docker-compose.yml').write_text(json.dumps(self.layout))
        self.objects=[{'Id':name+'-original-id','Name':'/'+name,'Image':'sha256:'+str(index)*64,
            'State':{'Running':True},'Config':{'Image':service['image'],'Labels':{
                'com.docker.compose.service':name,'com.docker.compose.project.working_dir':str(self.install)}},
            'Mounts':[dict(Type='bind',Source=volume['source']) for volume in service['volumes']]}
            for index,(name,service) in enumerate(self.layout['services'].items(),1)]
        self.tags={obj['Config']['Image']:obj['Image'] for obj in self.objects}

    def api(self,suffix):
        return {'harbor_version':self.version+'-fixture','status':'healthy'}

    def command(self,args,**kwargs):
        args=[str(a) for a in args];self.trace.append(args)
        if args[0]==str(self.compose):
            if 'config' in args:
                return (self.install/'docker-compose.yml').read_text()
            if 'stop' in args:
                if self.fault=='stop':raise migration.Refused('fixture stop failed')
                names=set(args[args.index('stop')+1:])&set(self.layout['services'])
                for obj in self.objects:
                    if not names or obj['Config']['Labels']['com.docker.compose.service'] in names:
                        obj['State']['Running']=False
                return ''
            if 'up' in args:
                self.layout=json.loads((self.install/'docker-compose.yml').read_text())
                self.version=next(iter(self.layout['services'].values()))['image'].split(':')[1]
                selected=args[args.index('-d')+1:]
                names=set(selected)&set(self.layout['services'])
                if not names:names=set(self.layout['services'])
                if 'postgresql' in names and 'log' not in names and not any(
                        o['Name']=='/log' and o['State']['Running'] for o in self.objects):
                    raise migration.Refused('syslog listener unavailable: localhost:1514')
                if self.fault=='export-db-start' and names=={'postgresql'}:
                    raise migration.Refused('fixture database start failed')
                if 'postgresql' in names:(self.data/'database').mkdir(exist_ok=True)
                existing={o['Name'].removeprefix('/'):o for o in self.objects}
                selected=[{'Id':existing.get(service['container_name'],{}).get('Id',name+'-'+self.version),'Name':'/'+service['container_name'],
                    'Image':self.tags.get(service['image'],'sha256:target'),
                    'State':{'Running':True,'Health':{'Status':'healthy'}},
                    'Config':{'Image':service['image'],'Labels':{'com.docker.compose.service':name,
                        'com.docker.compose.project.working_dir':str(self.install)}},
                    'Mounts':[dict(Type='bind',Source=v['source']) for v in service['volumes']]}
                    for name,service in self.layout['services'].items() if name in names]
                self.objects=[o for o in self.objects if o['Name'].removeprefix('/') not in names]+selected
                return ''
            raise AssertionError(args)
        if args[0]==str(self.docker):
            if args[1:3]==['ps','-aq'] and '--filter' in args:
                name=args[-1].removeprefix('name=^/').removesuffix('$')
                return self.prepares.get(name,'')
            if args[1:3]==['ps','-aq']:return '\n'.join(o['Id'] for o in self.objects)
            if args[1]=='inspect':return json.dumps(self.objects)
            if args[1:3]==['image','inspect']:
                ids={obj['Config']['Image']:obj['Image'] for obj in self.objects}
                return json.dumps([{'Architecture':self.arch,'Os':'linux','Size':10,'Id':ids[name]} for name in args[3:]])
            if args[1:3]==['image','save']:
                if self.fault=='save':raise migration.Refused('fixture save failed')
                Path(args[args.index('-o')+1]).write_bytes(b'original Docker image archive fixture')
                return ''
            if args[1:3]==['image','ls']:
                return '\n'.join(name+' '+identity for name,identity in self.tags.items())
            if args[1:3]==['image','load']:
                path=Path(args[-1])
                if path.name!='images.tar':
                    with tarfile.open(path) as archive:
                        for row in json.load(archive.extractfile('manifest.json')):
                            raw=archive.extractfile(row['Config']).read()
                            for tag in row['RepoTags']:
                                self.tags[tag]='sha256:'+migration.hashlib.sha256(raw).hexdigest()
                return ''
            if args[1]=='exec':
                if 'redis-cli' in args or 'valkey-cli' in args:
                    if args[-2:]==['INFO','server']:
                        return 'valkey_version:9.0.3\n' if 'valkey-cli' in args else 'redis_version:7.2.4\n'
                    if args[-2:]==['INFO','persistence']:return 'rdb_last_bgsave_status:ok\nrdb_changes_since_last_save:0\n'
                    if args[-2:]==['INFO','keyspace']:return 'db1:keys=1,expires=0,avg_ttl=0\n'
                    if 'CONFIG' in args:return args[-1]+'\n'+{'dir':'/var/lib/redis','dbfilename':'dump.rdb','appendonly':'no'}[args[-1]]+'\n'
                    if args[-1]=='SAVE':
                        if self.fault=='queue-save':return 'ERR fixture queue failed'
                        header=b'VALKEY080' if 'valkey-cli' in args else b'REDIS0011'
                        if self.fault=='queue-header':header=b'INVALID00'
                        (self.data/'redis/dump.rdb').write_bytes(header+b' fixture persisted queue')
                        return 'OK\n'
                    raise AssertionError(args)
                if 'psql' in args:
                    sql=args[-1]
                    if sql=='SHOW server_version_num':return '150010\n' if self.version=='v2.13.0' else '180000\n'
                    if 'FROM pg_database' in sql:return 'registry\n'
                    if 'FROM pg_tablespace' in sql:return 'custom\n' if self.fault=='custom-tablespace' else ''
                    if 'FROM pg_db_role_setting' in sql:return '1\n' if self.fault=='global-role-setting' else ''
                    if 'FROM pg_roles' in sql:return 'postgres\n'
                    if 'FROM schema_migrations' in sql:return '160|f\n'
                    raise AssertionError(args)
                if 'pg_dump' in args:
                    self.dump=b'PGDMP portable logical database fixture'
                    return ''
                if 'pg_restore' in args:
                    if self.fault=='restore-logical':raise migration.Refused('fixture logical restore failed')
                    (self.data/'database/restored-logical').write_bytes(b'confirmed portable database')
                    return ''
                if self.fault=='reindex':raise migration.Refused('fixture reindex failed')
                return ''
            if args[1]=='cp':
                if ':' in args[2]:Path(args[3]).write_bytes(self.dump)
                return ''
            if args[1]=='run':
                self.prepares[args[args.index('--name')+1]]=args[args.index('--name')+1]+'-id'
                image=next(a for a in args if a.startswith('goharbor/prepare:'))
                version=image.split(':')[1]
                if 'migrate' in args:
                    if self.fault=='migrate':raise migration.Refused('fixture migration failed')
                    (self.install/'harbor.yml').write_text('data_volume: '+str(self.data)+'\n_version: '+version[1:]+'\n')
                else:
                    if self.fault=='prepare':raise migration.Refused('fixture prepare failed')
                    self.version=version
                    for service in self.layout['services'].values():
                        service['image']=service['image'].split(':')[0]+':'+version
                    if version=='v2.16.0':
                        self.layout['services']['redis']['image']='goharbor/valkey-photon:'+version
                    (self.install/'docker-compose.yml').write_text(json.dumps(self.layout))
                return ''
            if args[1]=='stop':
                for obj in self.objects:
                    if obj['Id'] in args[2:] or obj['Name'].removeprefix('/') in args[2:]:obj['State']['Running']=False
                return ''
            if args[1]=='rm':
                self.objects=[o for o in self.objects if o['Id'] not in args[2:]]
                self.prepares={name:identity for name,identity in self.prepares.items() if identity not in args[2:]}
                return ''
            raise AssertionError(args)
        if args[0]=='tar':
            if any(o['State']['Running'] for o in self.objects):raise AssertionError('live snapshot')
        return super().command(args,**kwargs)


class HarborMigrationContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='harbor-state-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.harbor=Fixture(self.root)

    def test_official_compose_mount_trailing_slash_is_the_same_data_path(self):
        layout=deepcopy(self.harbor.layout)
        for service in layout['services'].values():
            for mount in service['volumes']:mount['source']+='/'
        with patch.object(self.harbor,'command',return_value=json.dumps(layout)):
            self.assertEqual(self.harbor.composition(),layout)

    def test_wrong_or_untyped_persistent_mount_source_is_refused(self):
        for source in [str(self.root/'unowned'),None,123,[],str(self.harbor.data/'..'/'data')]:
            with self.subTest(source=source):
                layout=deepcopy(self.harbor.layout)
                layout['services']['core']['volumes'][0]['source']=source
                with patch.object(self.harbor,'command',return_value=json.dumps(layout)):
                    with self.assertRaisesRegex(migration.Refused,'persistent Compose mount'):
                        self.harbor.composition()

    def test_native_compose_preserves_official_trailing_source_slash_without_a_daemon(self):
        binary=os.environ.get('UPGRADE_COMPOSE_FIXTURE')
        if not binary:self.skipTest('set UPGRADE_COMPOSE_FIXTURE to the qualified Compose binary')
        import yaml
        layout=deepcopy(self.harbor.layout)
        layout['services']['core']['volumes'][0]['source']+='/'
        (self.harbor.install/'docker-compose.yml').write_text(yaml.safe_dump(layout))
        result=subprocess.run([binary,'-f',str(self.harbor.install/'docker-compose.yml'),
            'config','--format','json'],capture_output=True,text=True,timeout=15,check=True)
        actual=json.loads(result.stdout)
        self.assertEqual(actual['services']['core']['volumes'][0]['source'],str(self.harbor.data)+'/')
        with patch.object(self.harbor,'command',return_value=result.stdout):
            self.assertEqual(self.harbor.composition(),actual)

    def test_real_archive_preserves_confirmed_data_keys_installer_and_private_identity(self):
        with self.harbor.lease():result=self.harbor.backup_source()
        self.assertEqual(result['marker'],'HARBOR_STOPPED_BACKUP_PASS')
        self.assertFalse(any(o['State']['Running'] for o in self.harbor.objects))
        receipt=self.harbor.load();self.assertEqual(receipt['phase'],'backup-complete')
        with tarfile.open(self.harbor.backup/'data.tar') as archive:
            self.assertEqual(archive.extractfile('data/registry/confirmed-blob').read(),b'original confirmed image bytes')
            self.assertEqual(archive.extractfile('data/secret/keys/secretkey').read(),b'private-original-encryption-key')
            self.assertIn('data/harbor/harbor.yml',archive.getnames())
            self.assertFalse(any('backup' in p for p in archive.getnames()))
        self.assertNotIn('private-',json.dumps(result))
        self.assertEqual(self.harbor.backup.stat().st_mode&0o777,0o700)
        self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in self.harbor.backup.iterdir()))
        original=(self.harbor.backup/'data.tar').read_bytes()
        with self.assertRaisesRegex(migration.Refused,'already exists'):self.harbor.backup_source()
        self.assertEqual((self.harbor.backup/'data.tar').read_bytes(),original)

    def test_backup_inside_data_and_symbolic_paths_refuse_before_commands(self):
        self.harbor.config['data_volume']=str(self.harbor.data)
        with self.assertRaisesRegex(migration.Refused,'outside'):
            migration.Harbor(data_directory=str(self.harbor.data),backup_directory=str(self.harbor.data/'backup'),
                binary_directory=str(self.root/'bin'),configuration=self.harbor.config,ca_file=str(self.root/'tls/ca'))
        link=self.root/'link';link.symlink_to(self.harbor.data,target_is_directory=True)
        with self.assertRaisesRegex(migration.Refused,'canonical'):
            migration.real_path(str(link))

    def test_foreign_mount_missing_member_or_external_storage_never_stops_source(self):
        foreign=deepcopy(self.harbor.objects[0]);foreign.update(Id='foreign',Name='/unrelated')
        self.harbor.objects.append(foreign)
        with self.assertRaisesRegex(migration.Refused,'foreign container'):self.harbor.backup_source()
        self.assertFalse(self.harbor.backup.exists())
        self.harbor.objects.pop();self.harbor.objects.pop()
        with self.assertRaisesRegex(migration.Refused,'incomplete'):self.harbor.backup_source()
        self.assertFalse(self.harbor.backup.exists())

    def test_external_services_or_storage_refuse_in_constructor(self):
        for extra in [{'external_database':{'harbor':{'host':'db.invalid'}}},
                {'external_redis':{'host':'redis.invalid'}},
                {'storage_service':{'s3':{'bucket':'private'}}},
                {'internal_tls':{'enabled':True}}]:
            with self.subTest(extra=list(extra)):
                config={**self.harbor.config,**extra}
                with self.assertRaises(migration.Refused):
                    migration.Harbor(data_directory=str(self.harbor.data),backup_directory=str(self.harbor.backup),
                        binary_directory=str(self.root/'bin'),configuration=config,ca_file=str(self.root/'tls/ca'))
        self.assertFalse(any(a[-1]=='stop' for a in self.harbor.trace))

    def test_no_space_no_snapshot_no_stop(self):
        with patch.object(migration.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(migration.Refused,'free space'):self.harbor.backup_source()
        self.assertFalse(self.harbor.backup.exists())
        self.assertFalse(any(a[-1]=='stop' for a in self.harbor.trace))

    def test_links_and_nested_external_state_refuse_without_partial_backup(self):
        (self.harbor.data/'external').symlink_to(self.root/'outside')
        with self.assertRaisesRegex(migration.Refused,'links'):self.harbor.backup_source()
        self.assertFalse(self.harbor.backup.exists())

    def test_stop_or_save_failure_preserves_real_checkpoint_without_success(self):
        self.harbor.fault='stop'
        with self.assertRaisesRegex(migration.Refused,'stop failed'):self.harbor.backup_source()
        receipt=json.loads(self.harbor.receipt.read_text())
        self.assertEqual(receipt['phase'],'stop-intent')
        self.assertFalse((self.harbor.backup/'data.tar').exists())

    def test_queue_save_follows_writer_stop_and_precedes_full_snapshot(self):
        self.harbor.backup_source()
        save=next(i for i,a in enumerate(self.harbor.trace) if a[-1]=='SAVE')
        stops=[i for i,a in enumerate(self.harbor.trace) if a[0]==str(self.harbor.compose) and 'stop' in a]
        archive=next(i for i,a in enumerate(self.harbor.trace) if a[0]=='tar')
        self.assertLess(stops[0],save);self.assertLess(save,stops[1]);self.assertLess(stops[1],archive)
        self.assertNotIn('redis',self.harbor.trace[stops[0]])
        receipt=self.harbor.load()
        self.assertEqual(receipt['queue']['version'],'7.2.4')
        self.assertEqual(receipt['queue']['rdb_sha256'],migration.digest(self.harbor.data/'redis/dump.rdb'))

    def test_queue_save_error_cannot_produce_an_incomplete_successful_backup(self):
        self.harbor.fault='queue-save'
        with self.assertRaisesRegex(migration.Refused,'queue snapshot failed'):
            self.harbor.backup_source()
        self.assertFalse((self.harbor.backup/'data.tar').exists())
        self.assertEqual(json.loads(self.harbor.receipt.read_text())['phase'],'stop-intent')
        running={o['Config']['Labels']['com.docker.compose.service'] for o in self.harbor.objects if o['State']['Running']}
        self.assertEqual(running,{'redis','postgresql','log'})

    def test_archive_corruption_and_foreign_host_refuse_reuse(self):
        self.harbor.backup_source()
        receipt=json.loads(self.harbor.receipt.read_text())
        receipt['machine']='foreign';migration.atomic_json(self.harbor.receipt,receipt)
        with self.assertRaisesRegex(migration.Refused,'another host'):self.harbor.load()
        receipt['machine']=migration.digest('/etc/machine-id');migration.atomic_json(self.harbor.receipt,receipt)
        (self.harbor.backup/'data.tar').write_bytes(b'corrupt')
        with self.assertRaisesRegex(migration.Refused,'changed'):self.harbor.load()

    def test_real_restore_preserves_failed_tree_and_recovers_original_confirmed_bytes(self):
        self.harbor.backup_source()
        data=self.harbor.data/'registry/confirmed-blob';data.write_bytes(b'new state after migration')
        result=self.harbor.restore()
        self.assertEqual(result['marker'],'HARBOR_ORIGINAL_RESTORE_PASS')
        self.assertEqual(data.read_bytes(),b'original confirmed image bytes')
        self.assertEqual((Path(result['quarantine'])/'registry/confirmed-blob').read_bytes(),b'new state after migration')
        self.assertEqual((self.harbor.data/'secret/keys/secretkey').read_text(),'private-original-encryption-key')
        self.assertEqual(self.harbor.load()['phase'],'restored')
        self.assertTrue(all(o['State']['Running'] for o in self.harbor.objects))

    def test_incomplete_backup_refuses_restore_and_never_touches_live_state(self):
        self.harbor.fault='save'
        with self.assertRaisesRegex(migration.Refused,'save failed'):self.harbor.backup_source()
        before=len(self.harbor.trace)
        with self.assertRaisesRegex(migration.Refused,'incomplete'):self.harbor.restore()
        self.assertEqual(len(self.harbor.trace),before)

    def packages(self):
        hashes={}
        for version in [migration.BRIDGES[self.harbor.arch],'v2.16.0']:
            images=io.BytesIO();manifest=[]
            with tarfile.open(fileobj=images,mode='w:gz') as archive:
                for name in self.harbor.layout['services']:
                    raw=json.dumps({'architecture':self.harbor.arch,'os':'linux','fixture':name,'version':version}).encode()
                    filename=migration.hashlib.sha256(raw).hexdigest()+'.json'
                    info=tarfile.TarInfo(filename);info.size=len(raw);archive.addfile(info,io.BytesIO(raw))
                    manifest.append({'Config':filename,'RepoTags':['goharbor/'+name+':'+version],'Layers':[]})
                raw=json.dumps(manifest).encode();info=tarfile.TarInfo('manifest.json');info.size=len(raw)
                archive.addfile(info,io.BytesIO(raw))
            path=self.root/(version+'.tgz')
            with tarfile.open(path,'w:gz') as archive:
                for name in ['harbor/prepare','harbor/install.sh','harbor/harbor.'+version+'.tar.gz']:
                    raw=images.getvalue() if name.endswith('.tar.gz') else b'fixed offline installer contract fixture'
                    info=tarfile.TarInfo(name);info.size=len(raw)
                    archive.addfile(info,io.BytesIO(raw))
            self.harbor.package_paths[version]=path
            hashes[version]={self.harbor.arch:migration.digest(path)}
        return patch.object(migration,'PACKAGES',hashes)

    def test_bridge_starts_core_without_proxy_or_jobs_and_checks_each_boundary(self):
        self.harbor.backup_source()
        with self.packages():result=self.harbor.upgrade()
        self.assertEqual(result['marker'],'HARBOR_OFFICIAL_MIGRATION_PASS')
        self.assertEqual(self.harbor.load()['phase'],'v2.16.0-ready')
        starts=[a for a in self.harbor.trace if a[0]==str(self.harbor.compose) and 'up' in a]
        self.assertEqual(len(starts),4)
        for args in [starts[0],starts[2]]:
            self.assertIn('core',args);self.assertNotIn('proxy',args);self.assertNotIn('jobservice',args)
        self.assertEqual(starts[1][-2:],['log','postgresql'])
        self.assertNotIn('core',starts[1])
        reindex=next(a for a in self.harbor.trace if 'reindexdb' in a)
        start_positions=[i for i,a in enumerate(self.harbor.trace)
                         if a[0]==str(self.harbor.compose) and 'up' in a]
        self.assertLess(start_positions[1],self.harbor.trace.index(reindex))
        self.assertLess(self.harbor.trace.index(reindex),start_positions[2])
        self.assertIn('--wait',starts[-1])
        runs=[a for a in self.harbor.trace if len(a)>1 and a[1]=='run']
        self.assertEqual([next(v for v in a if v.startswith('goharbor/prepare:')) for a in runs],
            ['goharbor/prepare:v2.14.6']*2+['goharbor/prepare:v2.16.0']*2)
        for args in runs:
            name=args[args.index('--name')+1]
            self.assertIn([str(self.harbor.docker),'ps','-aq','--filter','name=^/'+name+'$'],self.harbor.trace)
            self.assertIn([str(self.harbor.docker),'rm','-f',name+'-id'],self.harbor.trace)
        self.assertEqual((self.harbor.data/'registry/confirmed-blob').read_bytes(),b'original confirmed image bytes')
        self.assertEqual((self.harbor.data/'secret/keys/secretkey').read_text(),'private-original-encryption-key')

    def test_missing_or_wrong_platform_bridge_refuses_before_source_removal(self):
        self.harbor.backup_source();before=len(self.harbor.trace)
        with self.assertRaisesRegex(migration.Refused,'missing or corrupt'):self.harbor.upgrade()
        self.assertEqual(len(self.harbor.trace),before)
        self.assertEqual(self.harbor.load()['phase'],'backup-complete')
        with self.packages(),patch.object(self.harbor,'arch','arm64'):
            with self.assertRaisesRegex(migration.Refused,'approved offline bridge'):self.harbor.package('v2.14.6')

    def test_bridge_alias_uses_managed_host_architecture_and_rejects_conflicts(self):
        parent=self.root/'arm-managed';parent.mkdir()
        path=str(self.root/'bridge.tgz')
        with patch.object(migration.platform,'machine',return_value='aarch64'):
            instance=Fixture(parent,packages={'bridge':path})
        self.assertEqual(instance.package_paths,{'v2.14.4':Path(path)})
        parent=self.root/'conflicting-managed';parent.mkdir()
        with patch.object(migration.platform,'machine',return_value='aarch64'):
            with self.assertRaisesRegex(migration.Refused,'conflicting bridge'):
                Fixture(parent,packages={'bridge':path,'v2.14.4':str(self.root/'other.tgz')})

    def test_arm_bridge_keeps_original_data_permissions_and_full_restore_exit(self):
        self.harbor.arch='arm64'
        self.harbor.backup_source()
        with self.packages():result=self.harbor.upgrade()
        self.assertEqual(result['architecture'],'arm64')
        self.assertEqual(self.harbor.load()['phase'],'v2.16.0-ready')
        prepares=[v for args in self.harbor.trace if args[1:2]==['run']
                  for v in args if v.startswith('goharbor/prepare:')]
        self.assertEqual(prepares,['goharbor/prepare:v2.14.4']*2+['goharbor/prepare:v2.16.0']*2)
        self.assertTrue(all(o['State']['Running'] for o in self.harbor.objects))
        self.assertEqual(self.harbor.restore()['marker'],'HARBOR_ORIGINAL_RESTORE_PASS')
        self.assertEqual(self.harbor.version,'v2.13.0')
        self.assertEqual((self.harbor.data/'registry/confirmed-blob').read_bytes(),b'original confirmed image bytes')

    def test_arm_bridge_failure_checkpoint_remains_restorable(self):
        self.harbor.arch='arm64'
        self.harbor.backup_source()
        original=self.harbor.stage_release
        def fail_target(version,*args,**kwargs):
            if version=='v2.16.0':raise migration.Refused('target prepare refused')
            return original(version,*args,**kwargs)
        with self.packages(),patch.object(self.harbor,'stage_release',side_effect=fail_target):
            with self.assertRaisesRegex(migration.Refused,'target prepare refused'):
                self.harbor.upgrade()
        self.assertEqual(self.harbor.load()['phase'],'v2.14.4-ready')
        self.assertFalse(any(o['State']['Running'] for o in self.harbor.objects))
        self.assertEqual(self.harbor.restore()['marker'],'HARBOR_ORIGINAL_RESTORE_PASS')

    def test_third_party_arm_bridge_is_not_accepted_on_amd64(self):
        with self.assertRaisesRegex(migration.Refused,'approved offline bridge'):
            self.harbor.package('v2.14.4')

    def test_migration_failure_never_starts_services_and_keeps_restore_exit(self):
        self.harbor.backup_source();self.harbor.fault='migrate'
        with self.packages(),self.assertRaisesRegex(migration.Refused,'migration failed'):
            self.harbor.upgrade()
        self.assertEqual(self.harbor.load()['phase'],'upgrade-intent')
        self.assertFalse(any('up' in a for a in self.harbor.trace))
        self.assertTrue((self.harbor.backup/'data.tar').is_file())
        self.assertTrue(any(len(a)>2 and a[1:3]==['rm','-f'] for a in self.harbor.trace))
        # No generated Compose exists yet: restore falls back to the private
        # original composition and rebuilds original containers, not old IDs.
        result=self.harbor.restore()
        self.assertEqual(result['marker'],'HARBOR_ORIGINAL_RESTORE_PASS')

    def test_receipt_permissions_symlink_and_original_input_changes_refuse(self):
        self.harbor.backup_source();self.harbor.receipt.chmod(0o644)
        with self.assertRaisesRegex(migration.Refused,'private original backup receipt'):self.harbor.load()
        self.harbor.receipt.chmod(0o600)
        self.harbor.config['hostname']='another.invalid'
        with self.assertRaisesRegex(migration.Refused,'configuration'):self.harbor.load()

    def test_reindex_failure_preserves_recovery_even_with_invalid_live_compose(self):
        self.harbor.backup_source();self.harbor.fault='reindex'
        with self.packages(),self.assertRaisesRegex(migration.Refused,'reindex failed'):
            self.harbor.upgrade()
        receipt=self.harbor.load()
        self.assertEqual(receipt['phase'],'upgrade-intent')
        self.assertEqual(receipt['active_version'],'v2.16.0')
        self.assertEqual({o['Name'] for o in self.harbor.objects},{'/log','/postgresql'})
        (self.harbor.install/'docker-compose.yml').write_text('invalid failed generated configuration')
        result=self.harbor.restore()
        self.assertEqual(result['marker'],'HARBOR_ORIGINAL_RESTORE_PASS')
        self.assertEqual(self.harbor.version,'v2.13.0')
        self.assertTrue(all(o['State']['Running'] for o in self.harbor.objects))

    def test_existing_target_tag_conflict_never_loads_or_overwrites_that_image(self):
        self.harbor.backup_source()
        self.harbor.tags['goharbor/core:v2.14.6']='sha256:unrelated-tag-content'
        with self.packages(),self.assertRaisesRegex(migration.Refused,'will not overwrite'):
            self.harbor.upgrade()
        self.assertFalse(any(a[1:3]==['image','load'] for a in self.harbor.trace))
        self.assertEqual(self.harbor.tags['goharbor/core:v2.14.6'],'sha256:unrelated-tag-content')
        self.assertEqual(self.harbor.load()['phase'],'upgrade-intent')

    def test_timeout_stops_real_child_and_keeps_partial_output_private(self):
        self.harbor.backup.mkdir(mode=0o700)
        script='import time,sys;print("private partial diagnostic",flush=True);time.sleep(60)'
        with self.assertRaisesRegex(migration.Refused,'timed out'):
            migration.Harbor.command(self.harbor,[sys.executable,'-c',script],timeout=0.15)
        self.assertIn('private partial diagnostic',(self.harbor.backup/'commands.log').read_text())

    def test_sigterm_reaps_real_command_and_restores_signal_handler(self):
        self.harbor.backup.mkdir(mode=0o700)
        pidfile=self.root/'child.pid'
        script=('import os,time;from pathlib import Path;'
                'Path('+repr(str(pidfile))+').write_text(str(os.getpid()));'
                'print("private interrupted diagnostic",flush=True);time.sleep(60)')
        original=signal.getsignal(signal.SIGTERM)
        with migration.interruptible():
            # A real helper signals this process after the child has started.
            notifier=subprocess.Popen([sys.executable,'-c',
                'import os,signal,time;from pathlib import Path;'
                'p=Path('+repr(str(pidfile))+');deadline=time.monotonic()+5\n'
                'while not p.exists() and time.monotonic()<deadline:time.sleep(.01)\n'
                'os.kill('+str(os.getpid())+',signal.SIGTERM)'])
            try:
                with self.assertRaisesRegex(migration.Refused,'maintenance interrupted'):
                    migration.Harbor.command(self.harbor,[sys.executable,'-c',script],timeout=10)
            finally:
                notifier.wait(timeout=10)
        self.assertEqual(signal.getsignal(signal.SIGTERM),original)
        with self.assertRaises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)
        self.assertIn('private interrupted diagnostic',(self.harbor.backup/'commands.log').read_text())

    def test_api_checks_local_listener_with_original_tls_name_and_ca(self):
        self.harbor.config['harbor_admin_password']='private-original-password'
        response=io.BytesIO(b'{"harbor_version":"v2.16.0"}');response.status=200
        with patch.object(migration.ssl,'create_default_context') as tls, \
             patch.object(migration.socket,'create_connection') as connect, \
             patch.object(migration.http.client,'HTTPSConnection') as https:
            https.return_value.port=8443
            https.return_value.getresponse.return_value=response
            result=migration.Harbor.api(self.harbor,'systeminfo')
        self.assertEqual(result,{'harbor_version':'v2.16.0'})
        tls.assert_called_once_with(cafile=str(self.root/'tls/ca'))
        connect.assert_called_once_with(('127.0.0.1',8443),timeout=10)
        tls.return_value.wrap_socket.assert_called_once_with(connect.return_value,
            server_hostname='registry.example.invalid')
        https.return_value.request.assert_called_once_with('GET','/api/v2.0/systeminfo',headers={
            'Authorization':'Basic '+migration.base64.b64encode(b'admin:private-original-password').decode()})
        https.return_value.close.assert_called_once_with()

    def test_systeminfo_requires_authentication_before_reading_protected_version(self):
        self.harbor.config['harbor_admin_password']='private-original-password'
        connection=unittest.mock.Mock();connection.port=8443
        def response():
            headers=connection.request.call_args.kwargs.get('headers',{})
            # Official handler returns 200 with no protected fields anonymously.
            value={'harbor_version':'v2.13.0-db1569ae'} if headers.get('Authorization') else {'auth_mode':'db_auth'}
            stream=io.BytesIO(json.dumps(value).encode());stream.status=200
            return stream
        connection.getresponse.side_effect=response
        with patch.object(migration.ssl,'create_default_context'), \
                patch.object(migration.socket,'create_connection'), \
                patch.object(migration.http.client,'HTTPSConnection',return_value=connection):
            self.assertEqual(migration.Harbor.api(self.harbor,'systeminfo')['harbor_version'],'v2.13.0-db1569ae')
        self.assertFalse(self.harbor.backup.exists())

    def test_missing_version_credential_refuses_before_connection_or_source_mutation(self):
        with patch.object(migration.http.client,'HTTPSConnection') as connection:
            with self.assertRaisesRegex(migration.Refused,'administrator credential'):
                migration.Harbor.api(self.harbor,'systeminfo')
        connection.assert_not_called()
        self.assertFalse(self.harbor.backup.exists())
        self.assertTrue(all(obj['State']['Running'] for obj in self.harbor.objects))

    def test_portable_export_keeps_original_snapshot_and_starts_syslog_before_database(self):
        self.harbor.backup_source()
        original=(self.harbor.backup/'data.tar').read_bytes()
        result=self.harbor.portable_export()
        self.assertEqual(result['marker'],'HARBOR_PORTABLE_EXPORT_PASS')
        self.assertEqual((self.harbor.backup/'data.tar').read_bytes(),original)
        portable=json.loads((self.harbor.backup/'portable.json').read_text())
        self.assertEqual(portable['postgres_major'],15)
        self.assertNotIn('images.tar',portable['files'])
        self.assertIn('registry.dump',portable['files'])
        self.assertEqual(portable['schema_identity'],'160|f')
        self.assertFalse(any(o['State']['Running'] for o in self.harbor.objects))
        starts=[a for a in self.harbor.trace if a[0]==str(self.harbor.compose) and 'up' in a]
        self.assertEqual([a[-1] for a in starts],['log','postgresql'])
        self.assertTrue(all('--no-deps' in a for a in starts))
        self.assertNotIn('private-',json.dumps(result))
        self.assertEqual(self.harbor.load()['phase'],'portable-export-complete')
        self.assertEqual(self.harbor.restore()['marker'],'HARBOR_ORIGINAL_RESTORE_PASS')

    def valkey_source(self):
        self.harbor.version='v2.16.0'
        for service in self.harbor.layout['services'].values():
            service['image']=service['image'].split(':')[0]+':v2.16.0'
        self.harbor.layout['services']['redis']['image']='goharbor/valkey-photon:v2.16.0'
        (self.harbor.install/'docker-compose.yml').write_text(json.dumps(self.harbor.layout))
        for obj in self.harbor.objects:
            obj['Config']['Image']=self.harbor.layout['services'][obj['Name'][1:]]['image']
        self.harbor.tags={o['Config']['Image']:o['Image'] for o in self.harbor.objects}

    def test_official_valkey_903_rdb_survives_backup_export_and_original_restore(self):
        self.valkey_source()
        self.harbor.backup_source()
        receipt=self.harbor.load()
        self.assertEqual(receipt['queue']['engine'],'valkey')
        self.assertEqual(receipt['queue']['version'],'9.0.3')
        self.assertEqual((self.harbor.data/'redis/dump.rdb').read_bytes()[:9],b'VALKEY080')
        self.assertEqual(self.harbor.portable_export()['marker'],'HARBOR_PORTABLE_EXPORT_PASS')
        self.assertEqual(self.harbor.restore()['marker'],'HARBOR_ORIGINAL_RESTORE_PASS')
        self.assertEqual((self.harbor.data/'redis/dump.rdb').read_bytes()[:9],b'VALKEY080')

    def stopped_queue_checkpoint(self):
        self.harbor.fault='queue-header'
        with self.assertRaisesRegex(migration.Refused,'RDB snapshot header'):
            self.harbor.backup_source()
        self.harbor.fault=None
        self.assertEqual(json.loads(self.harbor.receipt.read_text())['phase'],'stopped')
        self.assertFalse(any(o['State']['Running'] for o in self.harbor.objects))
        self.harbor.trace.clear()

    def test_stopped_checkpoint_resumes_only_archive_without_restart_or_second_save(self):
        self.valkey_source();self.stopped_queue_checkpoint()
        rdb=self.harbor.data/'redis/dump.rdb';rdb.write_bytes(b'VALKEY080 original saved boundary')
        before=rdb.read_bytes()
        self.assertEqual(self.harbor.backup_source()['marker'],'HARBOR_STOPPED_BACKUP_PASS')
        self.assertEqual(rdb.read_bytes(),before)
        self.assertFalse(any('up' in a or 'SAVE' in a or 'stop' in a for a in self.harbor.trace))
        with self.assertRaisesRegex(migration.Refused,'already exists'):self.harbor.backup_source()

    def test_stopped_checkpoint_rejects_foreign_future_truncated_and_wrong_engine_headers(self):
        self.stopped_queue_checkpoint()
        rdb=self.harbor.data/'redis/dump.rdb'
        for header in [b'REDIS0012',b'REDIS0000',b'VALKEY080',b'VALKEY081',b'VALKEY079',b'REDIS001',b'INVALID00']:
            with self.subTest(header=header):
                rdb.write_bytes(header)
                with self.assertRaisesRegex(migration.Refused,'RDB snapshot header'):self.harbor.backup_source()
                self.assertFalse((self.harbor.backup/'data.tar').exists())
        rdb.write_bytes(b'REDIS0011 valid pinned Redis format')
        self.harbor.backup_source()

    def test_stopped_checkpoint_rejects_changed_config_container_tag_or_partial_output(self):
        self.stopped_queue_checkpoint()
        (self.harbor.data/'redis/dump.rdb').write_bytes(b'REDIS0011 original saved boundary')
        self.harbor.config['hostname']='changed.invalid'
        with self.assertRaisesRegex(migration.Refused,'configuration'):self.harbor.backup_source()
        self.harbor.config['hostname']='registry.example.invalid'
        obj=self.harbor.objects[0];identity=obj['Id'];obj['Id']='foreign-id'
        with self.assertRaisesRegex(migration.Refused,'source changed'):self.harbor.backup_source()
        obj['Id']=identity;obj['State']['Running']=True
        with self.assertRaises(migration.Refused):self.harbor.backup_source()
        obj['State']['Running']=False
        tag=obj['Config']['Image'];original=self.harbor.tags[tag];self.harbor.tags[tag]='changed'
        with self.assertRaisesRegex(migration.Refused,'source changed'):self.harbor.backup_source()
        self.harbor.tags[tag]=original
        for name in ['data.tar','images.tar','portable.json','registry.dump']:
            path=self.harbor.backup/name;path.write_bytes(b'partial original bytes')
            with self.assertRaisesRegex(migration.Refused,'already exists'):self.harbor.backup_source()
            self.assertEqual(path.read_bytes(),b'partial original bytes');path.unlink()
        self.assertFalse(any('up' in a or 'SAVE' in a for a in self.harbor.trace))

    def test_failed_export_start_stops_log_and_resumes_without_replacing_backup(self):
        self.harbor.backup_source()
        original=(self.harbor.backup/'data.tar').read_bytes()
        self.harbor.fault='export-db-start'
        with self.assertRaisesRegex(migration.Refused,'database start failed'):
            self.harbor.portable_export()
        self.assertFalse(any(o['State']['Running'] for o in self.harbor.objects))
        self.assertEqual(self.harbor.load()['phase'],'portable-export-intent')
        self.harbor.fault=None
        self.assertEqual(self.harbor.portable_export()['marker'],'HARBOR_PORTABLE_EXPORT_PASS')
        self.assertEqual((self.harbor.backup/'data.tar').read_bytes(),original)

    def test_export_retry_refuses_partial_output_before_starting_services(self):
        self.harbor.backup_source()
        self.harbor.checkpoint(self.harbor.load(),'portable-export-intent')
        (self.harbor.backup/'registry.dump').write_bytes(b'partial')
        self.harbor.trace.clear()
        with self.assertRaisesRegex(migration.Refused,'output already exists'):
            self.harbor.portable_export()
        self.assertFalse(any('up' in a for a in self.harbor.trace))

    def portable_target(self):
        self.harbor.backup_source();self.harbor.portable_export()
        self.harbor.transfer=self.harbor.backup
        source=self.root/'original-source-host-data'
        self.harbor.data.rename(source)
        self.harbor.backup=self.root/'target-checkpoint'
        self.harbor.receipt=self.harbor.backup/'receipt.json'
        self.harbor.objects=[]
        return source

    def test_portable_target_uses_logical_restore_before_bridge_core_and_preserves_source(self):
        source=self.portable_target()
        with self.packages():result=self.harbor.portable_import()
        self.assertEqual(result['marker'],'HARBOR_PORTABLE_IMPORT_PASS')
        self.assertEqual((source/'registry/confirmed-blob').read_bytes(),b'original confirmed image bytes')
        self.assertEqual((self.harbor.data/'registry/confirmed-blob').read_bytes(),b'original confirmed image bytes')
        self.assertFalse((self.harbor.data/'database/pg15/PG_VERSION').exists())
        self.assertEqual((self.harbor.data/'database/restored-logical').read_bytes(),b'confirmed portable database')
        restore=next(i for i,a in enumerate(self.harbor.trace) if 'pg_restore' in a)
        core=next(i for i,a in enumerate(self.harbor.trace) if a[0]==str(self.harbor.compose) and 'up' in a and 'core' in a)
        self.assertLess(restore,core)
        restore_command=self.harbor.trace[restore]
        self.assertIn('--create',restore_command)
        self.assertIn('--exit-on-error',restore_command)
        self.assertEqual(restore_command[restore_command.index('-d')+1],'postgres')
        self.assertNotIn('--single-transaction',restore_command)
        self.assertEqual(json.loads(self.harbor.receipt.read_text())['kind'],'harbor-portable-target')
        with self.packages(),self.assertRaisesRegex(migration.Refused,'clean target'):
            self.harbor.portable_import()

    def test_failed_portable_target_can_be_stopped_and_quarantined_without_source_mutation(self):
        source=self.portable_target();self.harbor.fault='restore-logical'
        with self.packages(),self.assertRaisesRegex(migration.Refused,'logical restore failed'):
            self.harbor.portable_import()
        result=self.harbor.abort_import()
        self.assertEqual(result['marker'],'HARBOR_PORTABLE_TARGET_STOPPED')
        self.assertFalse(self.harbor.data.exists())
        self.assertTrue(Path(result['quarantine']).is_dir())
        self.assertTrue((source/'database/pg15/PG_VERSION').is_file())
        self.assertFalse(self.harbor.objects)
        self.assertFalse(self.harbor.abort_import()['changed'])

    def test_portable_export_refuses_undumped_globals_and_returns_source_to_stopped(self):
        self.harbor.backup_source()
        for fault in ['custom-tablespace','global-role-setting']:
            with self.subTest(fault=fault):
                receipt=self.harbor.load()
                self.harbor.checkpoint(receipt,'backup-complete')
                self.harbor.fault=fault
                with self.assertRaisesRegex(migration.Refused,'tablespaces|cluster-wide'):
                    self.harbor.portable_export()
                self.assertFalse(any(o['State']['Running'] for o in self.harbor.objects))
                self.assertFalse(any('pg_dump' in a for a in self.harbor.trace))
                self.assertFalse((self.harbor.backup/'portable.json').exists())

    def test_prepare_failure_before_container_creation_preserves_first_error(self):
        original=self.harbor.command
        def command(args,**kwargs):
            if str(args[1])=='run':
                raise migration.Refused('fixture failed before daemon created a container')
            return original(args,**kwargs)
        with patch.object(self.harbor,'command',side_effect=command):
            with self.assertRaisesRegex(migration.Refused,'before daemon created'):
                self.harbor.prepare_container('v2.16.0',['prepare'],[])
        self.assertTrue(any('--filter' in a and a[-1].startswith('name=^/kubeauto-harbor-prepare-')
                            for a in self.harbor.trace))
        self.assertFalse(any(a[1:3]==['rm','-f'] for a in self.harbor.trace))

    def test_corrupt_transfer_and_wrong_architecture_packages_refuse_before_target_creation(self):
        self.portable_target()
        original=(self.harbor.transfer/'registry.dump').read_bytes()
        (self.harbor.transfer/'registry.dump').write_bytes(b'corrupt')
        with self.packages(),self.assertRaisesRegex(migration.Refused,'changed'):
            self.harbor.portable_import()
        self.assertFalse(self.harbor.backup.exists());self.assertFalse(self.harbor.data.exists())
        (self.harbor.transfer/'registry.dump').write_bytes(original)
        with self.packages(),patch.object(self.harbor,'arch','arm64'), \
             self.assertRaisesRegex(migration.Refused,'approved offline bridge'):
            self.harbor.portable_import()
        self.assertFalse(self.harbor.backup.exists());self.assertFalse(self.harbor.data.exists())

    def test_portable_queue_identity_mismatch_refuses_before_target_creation(self):
        self.portable_target()
        manifest=self.harbor.transfer/'portable.json'
        record=json.loads(manifest.read_text());record['queue']['rdb_sha256']='0'*64
        migration.atomic_json(manifest,record)
        with self.packages(),self.assertRaisesRegex(migration.Refused,'queue snapshot differs'):
            self.harbor.portable_import()
        self.assertFalse(self.harbor.backup.exists());self.assertFalse(self.harbor.data.exists())


if __name__=='__main__':unittest.main()
