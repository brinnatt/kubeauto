"""Offline MongoDB input, official API schema, security and ownership contracts."""
import base64
import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

import yaml
from jinja2 import Environment, StrictUndefined
from jsonschema import Draft7Validator

from common.constants import KubeConstant

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/cluster-addon'
spec = importlib.util.spec_from_file_location('mongodb_product', ROLE / 'files/mongodb.py')
product = importlib.util.module_from_spec(spec)
spec.loader.exec_module(product)


def configuration():
    c = yaml.safe_load((ROOT / 'conf/config.yml').read_text())
    kc = KubeConstant()
    values = {'operator_ver': kc.v_mongodb_operator, 'operator_chart_ver': kc.v_mongodb_operator_chart,
              'ver': kc.v_mongodb, 'backup_ver': kc.v_mongodb_backup,
              'fluentbit_ver': kc.v_mongodb_fluentbit, 'exporter_ver': kc.v_mongodb_exporter}
    c = {k: v for k, v in c.items() if k.startswith('mongodb_')}
    c.update({'mongodb_' + k: v for k, v in values.items()})
    c['mongodb_storage_class'] = 'mongodb-csi'
    return c


def render(c, template='cluster'):
    env = Environment(undefined=StrictUndefined)
    env.filters.update(bool=product.flag, to_json=json.dumps,
                       to_nice_yaml=lambda v, **kw: yaml.safe_dump(v, sort_keys=False, **kw))
    text = env.from_string((ROLE / f'templates/mongodb/{template}.yaml.j2').read_text()).render(c)
    return [d for d in yaml.safe_load_all(text) if d]


def check_operator_render(objects, namespace):
    deployment = next(o for o in objects if o['kind'] == 'Deployment')
    container = deployment['spec']['template']['spec']['containers'][0]
    env = {v['name']: v.get('value') for v in container['env']}
    if env.get('WATCH_NAMESPACE') != namespace or env.get('DISABLE_TELEMETRY') != 'true':
        raise ValueError('operator watch namespace/telemetry differs from the pinned Chart contract')
    if any(o['kind'] in ('ClusterRole', 'ClusterRoleBinding') for o in objects):
        raise ValueError('MongoDB operator must not receive global workload permissions')
    if not any(o['kind'] == 'Role' and o['metadata']['namespace'] == namespace for o in objects):
        raise ValueError('MongoDB operator requires an explicit namespace-local Role')


class FakeAPI:
    def __init__(self, c):
        self.c = c
        self.current = None
        self.foreign = False
        self.nodes = 7

    def get(self, kind, object_name='', namespace='', **kwargs):
        c = self.c
        if kind == 'namespace':
            labels = {} if self.foreign else product.LABELS
            return {'metadata': {'labels': labels}}
        if kind == 'secret':
            values = {}
            if object_name == c['mongodb_users_secret']:
                values = {f'MONGODB_{role}_{field}': (role if field == 'USER' else 'unit-test-only-password')
                          for role in product.SYSTEM_ROLES for field in ('USER', 'PASSWORD')}
            elif object_name == c['mongodb_encryption_key_secret']:
                values = {'encryption-key': base64.b64encode(bytes(range(32))).decode()}
            else:
                values = {'ca.crt': '-----BEGIN CERTIFICATE-----', 'tls.crt': '-----BEGIN CERTIFICATE-----', 'tls.key': 'unit-only-key'}
            return {'data': {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}}
        if kind == 'storageclass':
            return {'volumeBindingMode': 'WaitForFirstConsumer'}
        if kind == 'nodes':
            return {'items': [{'spec': {}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                              for _ in range(self.nodes)]}
        if kind == 'crd':
            return {'metadata': {}} if self.current else None
        if kind == 'psmdb':
            return self.current if object_name else {'items': [self.current] if self.current else []}
        if kind == 'psmdb-backup':
            return {'items': []}
        if kind == 'pvc':
            return {'items': []}
        if kind in ('service', 'networkpolicy', 'servicemonitor'):
            return None
        raise AssertionError(kind)


class MongoDBDeliveryTests(unittest.TestCase):
    def test_log_rotation_is_opt_in_and_uses_both_official_log_paths(self):
        c=configuration();self.assertNotIn('logrotate',render(c)[0]['spec']['logcollector'])
        self.assertNotIn('initContainerSecurityContext',render(c)[0]['spec'])
        c.pop('mongodb_logrotate',None);product.validate(c)
        policy={'interval_minutes':1,'max_size_mb':32,'retain':1}
        c['mongodb_logrotate']=policy;product.validate(c)
        for chart in ('1.23.1','1.23.2'):
            with self.subTest(chart=chart):
                operator='1.23.0' if chart=='1.23.1' else '1.23.1'
                obj=render(c|{'mongodb_operator_ver':operator,'mongodb_operator_chart_ver':chart})[0]
                with tarfile.open(ROLE / ('files/psmdb-operator-' + chart + '.tgz')) as archive:
                    crd=next(o for o in yaml.safe_load_all(archive.extractfile('psmdb-operator/crds/crd.yaml'))
                             if o['metadata']['name']=='perconaservermongodbs.psmdb.percona.com')
                schema=next(v['schema']['openAPIV3Schema'] for v in crd['spec']['versions'] if v['name']=='v1')
                Draft7Validator(schema).validate(obj)
                self.assertEqual(obj['spec']['initContainerSecurityContext'],{
                    'runAsUser':0,'runAsNonRoot':False,'allowPrivilegeEscalation':False,
                    'capabilities':{'drop':['ALL'],'add':['CHOWN','DAC_OVERRIDE']}})
                rotation=obj['spec']['logcollector']['logrotate']
                self.assertEqual(rotation['schedule'],'*/1 * * * *')
                conf=rotation['configuration']
                for role in ('mongod','mongos'):
                    self.assertIn('/data/db/logs/'+role+'.full.log {',conf)
                    self.assertIn("-name '"+role+".log.*' -mtime +7 -delete",conf)
                self.assertEqual(conf.count('maxsize 32M'),2);self.assertEqual(conf.count('rotate 1\n'),2)
                self.assertEqual(conf.count('nocompress'),2);self.assertEqual(conf.count('copytruncate'),2)
                self.assertEqual(conf.count('db.adminCommand({ logRotate: 1 })'),2)
                self.assertNotIn('mongodb://backup:',conf)
                with tempfile.TemporaryDirectory() as tmp:
                    path=Path(tmp,'rotation.conf');path.write_text(conf)
                    result=subprocess.run([shutil.which('logrotate') or '/sbin/logrotate','-d','-s',tmp+'/state',str(path)],capture_output=True,text=True)
                    self.assertEqual(result.returncode,0,result.stderr)
        for bad in (None,[],{'interval_minutes':1},policy|{'extra':True},policy|{'interval_minutes':0},
                    policy|{'interval_minutes':True},policy|{'interval_minutes':'*; exit 0'},
                    policy|{'max_size_mb':0},policy|{'max_size_mb':1025},policy|{'retain':0},policy|{'retain':31}):
            with self.subTest(invalid=bad),self.assertRaises(ValueError):
                product.validate(c|{'mongodb_logrotate':bad})
        with self.assertRaises(ValueError):product.validate(c|{'mongodb_logcollector_enabled':False})

    def test_operator_trust_preserves_active_and_retained_backup_ca_without_credentials(self):
        c = configuration() | {'mongodb_backup_enabled': True, 'mongodb_backup_ca_secret': 'current-ca'}
        api = FakeAPI(c)
        refs = lambda n: {'type': 'minio', 'minio': {'caBundle': {'name': n, 'key': 'ca.crt'}}}
        cluster = {'metadata': {'name': 'other', 'labels': product.LABELS},
                   'spec': {'backup': {'storages': {'main': refs('active-ca')}}}}
        backup = {'spec': {'clusterName': 'other'}, 'status': {'minio': refs('retained-ca')['minio']}}
        def get(kind, *args, **kwargs):
            if kind == 'crd':return {'metadata': {}}
            if kind == 'psmdb':return {'items': [cluster]}
            if kind == 'psmdb-backup':return {'items': [backup, {'spec': {'clusterName': 'foreign'},
                'status': {'minio': refs('foreign-ca')['minio']}}]}
            return {'data': {'ca.crt': base64.b64encode(('PUBLIC CA '+args[0]).encode()).decode(),
                             'password': base64.b64encode(b'private').decode()}}
        with patch.object(api, 'get', side_effect=get):
            trust = product.operator_trust(c, api)
        self.assertEqual([r['name'] for r in trust['refs']], ['active-ca', 'current-ca', 'retained-ca'])
        self.assertEqual(len(trust['sha256']), 64)
        self.assertNotIn('PUBLIC CA', json.dumps(trust));self.assertNotIn('private', json.dumps(trust))
        self.assertNotIn('foreign-ca', json.dumps(trust))
        with patch.object(api, 'get', side_effect=lambda kind,*a,**kw:
                {'data': {'ca.crt': base64.b64encode(b'changed').decode()}} if kind == 'secret' else get(kind,*a,**kw)):
            self.assertNotEqual(product.operator_trust(c, api)['sha256'], trust['sha256'])

    def test_operator_postrender_mounts_only_ca_keys_and_keeps_official_objects(self):
        c = configuration()
        obj = {'kind': 'Deployment', 'metadata': {'name': 'mongodb-operator', 'namespace': c['mongodb_namespace']},
               'spec': {'template': {'spec': {'containers': [{'name': 'psmdb-operator', 'env': [
                   {'name': 'WATCH_NAMESPACE', 'value': c['mongodb_namespace']}],
                   'image': 'registry.talkschool.cn:5000/brinnatt/percona-server-mongodb-operator:1.23.1'}]}}}}
        objects = [obj, {'kind': 'Role', 'metadata': {'name': 'unchanged'}}]
        original = copy.deepcopy(objects)
        trust = {'refs': [{'name': 'backup-ca', 'key': 'ca.crt'}], 'sha256': 'a'*64}
        result = product.operator_manifest(c, objects, trust)
        self.assertEqual(objects, original)
        self.assertEqual(result[1], original[1])
        spec = result[0]['spec']['template']['spec'];container = spec['containers'][0]
        self.assertEqual(spec['volumes'][0]['projected']['sources'][0]['secret']['items'][0]['key'], 'ca.crt')
        self.assertEqual(spec['volumes'][0]['projected']['sources'][0]['secret']['name'], 'backup-ca')
        self.assertTrue(container['volumeMounts'][0]['readOnly'])
        env = next(e for e in container['env'] if e['name'] == 'SSL_CERT_DIR')
        self.assertIn('/etc/kubeauto/mongodb-backup-ca', env['value'])
        self.assertEqual(result[0]['spec']['template']['metadata']['annotations']['kubeauto.io/backup-ca-sha256'], 'a'*64)
        self.assertEqual(product.operator_manifest(c, original, {'refs': [], 'sha256': 'b'*64}), original)
        for change in ('foreign', 'duplicate', 'conflict'):
            wrong = copy.deepcopy(original)
            if change == 'foreign':wrong[0]['metadata']['namespace'] = 'foreign'
            if change == 'duplicate':wrong.append(copy.deepcopy(wrong[0]))
            if change == 'conflict':wrong[0]['spec']['template']['spec']['containers'][0]['env'].append(
                {'name': 'SSL_CERT_DIR', 'value': '/foreign'})
            with self.subTest(change=change), self.assertRaises(ValueError):
                product.operator_manifest(c, wrong, trust)

    def test_backup_preparation_defers_every_schedule_until_agents_are_ready(self):
        c = configuration() | {'mongodb_backup_enabled': True, 'mongodb_backup_schedule_enabled': True,
                               'mongodb_pitr_enabled': True}
        desired = render(c)[0]
        for present in (False, True):
            with self.subTest(existing=present), tempfile.TemporaryDirectory() as directory:
                api = FakeAPI(c)
                current = copy.deepcopy(desired) if present else None
                if current:current['spec']['backup']['enabled'] = False
                events = []
                def run(*args, **kwargs):
                    if args[0] == 'create':return json.dumps(desired)
                    staged = json.loads(Path(args[args.index('-f') + 1]).read_text())
                    self.assertTrue(staged['spec']['backup']['enabled'])
                    self.assertTrue(all(t['enabled'] is False for t in staged['spec']['backup']['tasks']))
                    self.assertFalse(staged['spec']['backup']['pitr']['enabled'])
                    expected = copy.deepcopy(desired)
                    for task in expected['spec']['backup']['tasks']:task['enabled'] = False
                    expected['spec']['backup']['pitr']['enabled'] = False
                    self.assertEqual(staged, expected)
                    events.append('apply-agents-without-schedule')
                with patch.object(api, 'get', return_value=current), patch.object(api, 'run', create=True, side_effect=run), \
                        patch.object(product, 'wait_health', side_effect=lambda *a:events.append('wait-for-all-agents')):
                    product.prepare_backup(c, api, Path(directory) / 'cluster.yaml')
                self.assertEqual(events, ['apply-agents-without-schedule', 'wait-for-all-agents'])
                self.assertTrue(desired['spec']['backup']['tasks'][0]['enabled'])
        tasks = yaml.safe_load((ROLE / 'tasks/mongodb.yml').read_text())
        preparation = next(i for i,t in enumerate(tasks) if 'prepare-backup' in t.get('ansible.builtin.command', {}).get('argv', []))
        publication = next(i for i,t in enumerate(tasks) if t['name'] == '发布 MongoDB 声明')
        self.assertLess(preparation, publication)
        condition = tasks[preparation]['when']
        env = Environment(undefined=StrictUndefined)
        env.filters['bool'] = product.flag
        self.assertTrue(env.compile_expression(condition)(mongodb_backup_enabled=True))
        self.assertFalse(env.compile_expression(condition)(mongodb_backup_enabled=True, mongodb_backup_schedule_enabled=False))

    def test_backup_preparation_is_noop_for_disabled_schedule_or_ready_agents(self):
        c = configuration()
        for settings in ({'mongodb_backup_enabled': False}, {'mongodb_backup_enabled': True, 'mongodb_backup_schedule_enabled': False},
                         {'mongodb_backup_enabled': True, 'mongodb_backup_schedule_enabled': True}):
            c.update(settings)
            current = render(c)[0]
            api = FakeAPI(c)
            with patch.object(api, 'get', side_effect=[current, {'items': []}]), \
                    patch.object(product, 'backup_agents_ready', return_value=True), \
                    patch.object(api, 'run', create=True) as run, patch.object(product, 'wait_health') as wait:
                product.prepare_backup(c, api, Path('unused'))
                run.assert_not_called();wait.assert_not_called()

    def test_backup_agent_readiness_covers_every_data_member_including_configsvr(self):
        c = configuration() | {'mongodb_sharding_enabled': True,
                               'mongodb_replsets': [{'name': 'rs0', 'size': 3}, {'name': 'rs1', 'size': 3}]}
        pods = [{'metadata': {'labels': {'app.kubernetes.io/instance': c['mongodb_name'],
                    'app.kubernetes.io/replset': rs}}, 'status': {'containerStatuses': [{'name': 'backup-agent', 'ready': True}]}}
                for rs in ('rs0', 'rs1', 'cfg') for i in range(3)]
        self.assertTrue(product.backup_agents_ready(c, pods))
        self.assertFalse(product.backup_agents_ready(c, pods[:-1]))
        pods[-1]['status']['containerStatuses'][0]['ready'] = False
        self.assertFalse(product.backup_agents_ready(c, pods))
        pods[-1]['status']['containerStatuses'] = []
        self.assertFalse(product.backup_agents_ready(c, pods))

    def test_backup_preparation_rejects_foreign_identity_and_never_succeeds_after_agent_timeout(self):
        c = configuration() | {'mongodb_backup_enabled': True, 'mongodb_backup_schedule_enabled': True}
        desired = render(c)[0]
        api = FakeAPI(c)
        with patch.object(api, 'get', return_value={'metadata': {'labels': {}}}), patch.object(api, 'run', create=True) as run:
            with self.assertRaisesRegex(ValueError, 'foreign cluster'):product.prepare_backup(c, api, Path('manifest'))
            run.assert_not_called()
        def run(*args, **kwargs):
            return json.dumps(desired) if args[0] == 'create' else ''
        with patch.object(api, 'get', return_value=None), patch.object(api, 'run', create=True, side_effect=run), \
                patch.object(product, 'wait_health', side_effect=ValueError('agent timeout')), redirect_stdout(StringIO()) as output:
            with self.assertRaisesRegex(ValueError, 'agent timeout'):product.prepare_backup(c, api, Path('manifest'))
            self.assertNotIn('MONGODB_BACKUP_AGENTS_READY', output.getvalue())
        bad = copy.deepcopy(desired);bad['metadata']['name'] = 'foreign'
        with patch.object(api, 'get', return_value=None), patch.object(api, 'run', create=True, return_value=json.dumps(bad)) as run:
            with self.assertRaisesRegex(ValueError, 'manifest identity'):product.prepare_backup(c, api, Path('manifest'))
            self.assertEqual(run.call_count, 1)

    def setUp(self):
        self.c = configuration()

    def test_explicit_volume_growth_matches_both_pinned_crd_schemas_without_autoscaling(self):
        self.assertNotIn('storageScaling', render(self.c)[0]['spec'])
        old = self.c.copy();old.pop('mongodb_volume_expansion_enabled')
        product.validate(old)
        self.assertNotIn('storageScaling', render(old)[0]['spec'])
        self.c['mongodb_volume_expansion_enabled'] = True
        for chart in ('1.23.1', '1.23.2'):
            with tarfile.open(ROLE / ('files/psmdb-operator-' + chart + '.tgz')) as archive:
                crd = next(o for o in yaml.safe_load_all(archive.extractfile('psmdb-operator/crds/crd.yaml'))
                           if o['metadata']['name'] == 'perconaservermongodbs.psmdb.percona.com')
            schema = next(v['schema']['openAPIV3Schema'] for v in crd['spec']['versions'] if v['name'] == 'v1')
            obj = render(self.c)[0]
            Draft7Validator(schema).validate(obj)
            self.assertEqual(obj['spec']['storageScaling'], {'enableVolumeScaling': True, 'autoscaling': {'enabled': False}})
        for value in (None, 1, 'sometimes', []):
            with self.assertRaises(ValueError):product.validate(self.c | {'mongodb_volume_expansion_enabled': value})

    def test_volume_growth_requires_explicit_opt_in_and_expandable_backend_and_never_shrinks(self):
        self.c['mongodb_pvc_size'] = '4Gi'
        api = FakeAPI(self.c)
        revision = product.preflight(self.c, api)
        api.current = render(self.c)[0]
        api.current['metadata']['annotations']['kubeauto.io/encryption-key-sha256'] = revision['encryption_revision']
        self.c['mongodb_pvc_size'] = '5Gi'
        with self.assertRaisesRegex(ValueError, 'explicit mongodb_volume_expansion_enabled'):
            product.preflight(self.c, api)
        self.c['mongodb_volume_expansion_enabled'] = True
        with self.assertRaisesRegex(ValueError, 'allowVolumeExpansion=true'):product.preflight(self.c, api)
        original_get = api.get
        def expandable(kind, *args, **kwargs):
            return {'volumeBindingMode': 'WaitForFirstConsumer', 'allowVolumeExpansion': True} if kind == 'storageclass' else original_get(kind, *args, **kwargs)
        with patch.object(api, 'get', side_effect=expandable):
            product.preflight(self.c, api)
            self.c['mongodb_pvc_size'] = '3Gi'
            with self.assertRaisesRegex(ValueError, 'shrink is refused'):product.preflight(self.c, api)
            self.c.update(mongodb_pvc_size='4Gi', mongodb_volume_expansion_enabled=False)
            product.preflight(self.c, api)

    def test_expansion_health_waits_for_claim_capacity_and_filesystem_resize(self):
        self.c.update(mongodb_volume_expansion_enabled=True, mongodb_pvc_size='5Gi')
        cr = render(self.c)[0]
        cr['metadata']['generation'] = 2;cr['status'] = {'state': 'ready', 'observedGeneration': 2}
        pods = [{'metadata': {'name': 'member-'+str(i), 'labels': {'app.kubernetes.io/instance': self.c['mongodb_name'],
                    'app.kubernetes.io/replset': 'rs0'}}, 'spec': {'nodeName': 'node-'+str(i),
                    'volumes': [{'persistentVolumeClaim': {'claimName': 'claim-'+str(i)}}]},
                 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}} for i in range(3)]
        claims = [{'metadata': {'name': 'claim-'+str(i)}, 'status': {'phase': 'Bound', 'capacity': {'storage': '4Gi'}}}
                  for i in range(3)]
        api = FakeAPI(self.c)
        def get(kind, *args, **kwargs):
            return {'psmdb': cr, 'pods': {'items': pods}, 'pvc': {'items': claims}}[kind]
        with patch.object(api, 'get', side_effect=get), patch.object(api, 'run', create=True) as run:
            with self.assertRaisesRegex(ValueError, 'expansion has not completed'):product.health(self.c, api)
            run.assert_not_called()
            for claim in claims:claim['status']['capacity']['storage'] = '5Gi'
            claims[0]['status']['conditions'] = [{'type': 'FileSystemResizePending', 'status': 'True'}]
            with self.assertRaisesRegex(ValueError, 'expansion has not completed'):product.health(self.c, api)
            run.assert_not_called()
            claims[0]['status']['conditions'] = []
            run.return_value = json.dumps({'status': {}, 'config': {}, 'encryption': {'encryptionEnabled': True}})
            with patch.object(product, 'check_members'):product.health(self.c, api)
            run.assert_called_once()

    def test_crd_renderer_accepts_kubectl_per_object_json_and_list(self):
        with tarfile.open(ROLE / 'files/psmdb-operator-1.23.2.tgz') as archive:
            objects = [obj for obj in yaml.safe_load_all(archive.extractfile('psmdb-operator/crds/crd.yaml')) if obj]
        stream = '\n'.join(json.dumps(obj, indent=2) for obj in objects)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stream)  # Exact failure produced by kubectl v1.33.6.
        self.assertEqual(product.kubectl_json_objects(stream), objects)
        self.assertEqual(product.kubectl_json_objects(json.dumps({'kind': 'List', 'items': objects})), objects)
        api = FakeAPI(self.c)
        api.run = lambda *args, **kwargs: stream
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(StringIO()) as output:
                product.reconcile_crds(self.c, api, ROLE / 'files/psmdb-operator-1.23.2.tgz', Path(tmp) / 'input.json')
            self.assertIn('MONGODB_CRD_CONTRACT_PASS changed=0', output.getvalue())
        for invalid in ('', '[]', '{}\ninvalid', '{"kind":"List","items":[null]}'):
            with self.assertRaises(ValueError):
                product.kubectl_json_objects(invalid)

    def test_crd_upgrade_transfers_only_pinned_helm_fields_with_identity_and_dry_run(self):
        charts = {}
        for version in ('1.23.1', '1.23.2'):
            with tarfile.open(ROLE / 'files' / ('psmdb-operator-' + version + '.tgz')) as archive:
                charts[version] = {o['metadata']['name']: o for o in yaml.safe_load_all(
                    archive.extractfile('psmdb-operator/crds/crd.yaml'))}
        for drift in (None, 'schema', 'label', 'uid', 'revision', 'dry-run'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                current = copy.deepcopy(charts['1.23.1'])
                for name, obj in current.items():
                    obj['metadata'].update(uid='uid-' + name, resourceVersion='42',
                                           annotations={'customer.example/preserve': 'original'})
                sample = next(iter(current.values()))
                if drift == 'schema':sample['spec']['versions'][0]['schema']['openAPIV3Schema']['description'] = 'foreign'
                if drift == 'label':sample['metadata']['labels']['app.kubernetes.io/version'] = 'foreign'
                if drift == 'uid':sample['metadata'].pop('uid')
                if drift == 'revision':sample['metadata'].pop('resourceVersion')
                applied = []
                def run(*args, **kwargs):
                    if args[0] == 'create':return '\n'.join(json.dumps(o) for o in
                        yaml.safe_load_all(Path(args[args.index('-f') + 1]).read_text()))
                    if args[0] == 'get':return '{"items":[]}'
                    if args[0] == 'wait':return ''
                    self.assertEqual(args[0], 'apply')
                    if '--validate=false' not in args:
                        raise ValueError('client CRD validation requires group, names and scope on a partial object')
                    if '--force-conflicts' not in args:raise ValueError('conflicts with helm: spec.versions and version label')
                    objects = json.loads(Path(args[-1]).read_text())['items']
                    for obj in objects:
                        name = obj['metadata']['name']
                        self.assertEqual(set(obj['spec']), {'versions'})
                        self.assertEqual(obj['spec']['versions'], charts['1.23.2'][name]['spec']['versions'])
                        self.assertEqual(set(obj['metadata']), {'name', 'uid', 'resourceVersion', 'labels'})
                        self.assertEqual(obj['metadata']['uid'], 'uid-' + name)
                        self.assertEqual(obj['metadata']['resourceVersion'], '42')
                        self.assertEqual(set(obj['metadata']['labels']), {'app.kubernetes.io/version'})
                    dry = '--dry-run=server' in args
                    applied.append('dry-run' if dry else 'apply')
                    if drift == 'dry-run':raise ValueError('admission refused')
                    if not dry:
                        backups = list(Path(tmp).glob('crds-before-*.json'))
                        self.assertEqual(len(backups), 1)
                        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
                    return ''
                api = FakeAPI(self.c)
                api.get = lambda kind, name, **kwargs: current[name]
                api.run = run
                self.c['mongodb_crd_upgrade'] = True
                if drift:
                    with self.assertRaises(ValueError):product.reconcile_crds(
                        self.c, api, ROLE / 'files/psmdb-operator-1.23.2.tgz', Path(tmp) / 'input.json')
                    self.assertEqual(applied, ['dry-run'] if drift == 'dry-run' else [])
                else:
                    product.reconcile_crds(self.c, api, ROLE / 'files/psmdb-operator-1.23.2.tgz', Path(tmp) / 'input.json')
                    self.assertEqual(applied, ['dry-run', 'apply'])

    def test_default_disabled_and_image_catalog_publication_are_consistent(self):
        self.assertEqual(self.c['mongodb_install'], 'no')
        kc = KubeConstant()
        published = json.loads((ROOT / 'tests/evidence/mongodb-images-20261015.json').read_text())
        self.assertEqual(published['durable_rc'], 0)
        self.assertEqual(published['failure_markers'], 0)
        self.assertEqual(published['workflow_conclusion'], 'success')
        records = {r['key']: r for r in published['images']}
        for pin in kc.component_images['mongodb']:
            r = records[pin]
            self.assertEqual(r['dockerhub_digest'], r['talkedu_digest'])
            self.assertEqual(set(r['platforms']), {'linux/amd64', 'linux/arm64'})
        self.assertEqual(product.validate(self.c), None)

    def test_invalid_quorum_topology_tls_backup_and_resources_fail_before_api(self):
        invalid = [dict(mongodb_replsets=[]), dict(mongodb_replsets=[{'name': 'rs0', 'size': 2}]),
                   dict(mongodb_replsets=[{'name': 'rs0', 'size': 4}]),
                   dict(mongodb_replsets=[{'name': 'rs0', 'size': 3}, {'name': 'rs1', 'size': 3}]),
                   dict(mongodb_namespace='invalid;command'), dict(mongodb_ver='latest'),
                   dict(mongodb_cpu_request='3'), dict(mongodb_memory_request='0'),
                   dict(mongodb_cache_size_gb=3), dict(mongodb_pitr_enabled=True),
                   dict(mongodb_tls_internal_secret=self.c['mongodb_tls_secret']),
                   dict(mongodb_users=[{'name': 'app', 'password': 'unsafe'}]),
                   dict(mongodb_exporter_enabled=False, mongodb_service_monitor_enabled=True)]
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    product.validate(self.c | change)

    def test_operator_uses_official_namespace_local_rbac_mode(self):
        values = render(self.c, 'operator-values')[0]
        # The pinned Chart switches to ClusterRole/ClusterRoleBinding whenever
        # watchNamespace is non-empty, even with watchAllNamespaces=false.
        self.assertFalse(values['watchAllNamespaces'])
        self.assertFalse(values.get('watchNamespace'))

    def test_upgrade_chart_is_checksum_pinned_matches_operator_and_accepts_both_topologies(self):
        chart = ROLE / 'files/psmdb-operator-1.23.1.tgz'
        self.assertEqual(hashlib.sha256(chart.read_bytes()).hexdigest(),
                         '08ce50b0cbd582694790d201c9573d61a0e0b89d460961d8590cd1ee82039ccd')
        with tarfile.open(chart) as archive:
            metadata = yaml.safe_load(archive.extractfile('psmdb-operator/Chart.yaml'))
            crds = list(yaml.safe_load_all(archive.extractfile('psmdb-operator/crds/crd.yaml')))
        self.assertEqual(str(metadata['appVersion']), '1.23.0')
        schema = next(v for d in crds if d['metadata']['name'] == 'perconaservermongodbs.psmdb.percona.com'
                      for v in d['spec']['versions'] if v['name'] == 'v1')['schema']['openAPIV3Schema']
        c = self.c | {'mongodb_operator_ver': '1.23.0', 'mongodb_operator_chart_ver': '1.23.1',
                      'mongodb_ver': '8.0.26-11', 'mongodb_fluentbit_ver': '5.0.9-1'}
        for sharded in (False, True):
            c['mongodb_sharding_enabled'] = sharded
            if sharded:
                c['mongodb_replsets'] = [{'name': 'rs0', 'size': 3}, {'name': 'rs1', 'size': 3}]
            product.validate(c)
            Draft7Validator(schema).validate(render(c)[0])
        with self.assertRaisesRegex(ValueError, 'Operator and Chart'):
            product.validate(self.c | {'mongodb_operator_chart_ver': '1.23.1'})
        # Server patches can be upgraded after the Operator without changing FCV.
        product.validate(self.c | {'mongodb_ver': '8.0.26-11'})

    def test_helm_contract_accepts_literal_watch_namespace_and_rejects_global_binding(self):
        objects = [{'kind': 'Deployment', 'spec': {'template': {'spec': {'containers': [
            {'env': [{'name': 'WATCH_NAMESPACE', 'value': 'mongodb'},
                     {'name': 'DISABLE_TELEMETRY', 'value': 'true'}]}]}}}},
                   {'kind': 'Role', 'metadata': {'namespace': 'mongodb'}}]
        check_operator_render(objects, 'mongodb')
        with self.assertRaisesRegex(ValueError, 'global workload'):
            check_operator_render(objects + [{'kind': 'ClusterRoleBinding'}], 'mongodb')

    def test_official_chart_checksum_and_both_topology_schemas(self):
        chart = ROLE / 'files/psmdb-operator-1.23.2.tgz'
        self.assertEqual(hashlib.sha256(chart.read_bytes()).hexdigest(), KubeConstant().v_mongodb_operator_chart_sha256)
        with tarfile.open(chart) as archive:
            crds = list(yaml.safe_load_all(archive.extractfile('psmdb-operator/crds/crd.yaml')))
        crd = next(d for d in crds if d['metadata']['name'] == 'perconaservermongodbs.psmdb.percona.com')
        schema = next(v for v in crd['spec']['versions'] if v['name'] == 'v1')['schema']['openAPIV3Schema']
        for sharded in (False, True):
            c = copy.deepcopy(self.c)
            c['mongodb_sharding_enabled'] = sharded
            if sharded:
                c['mongodb_replsets'].append({'name': 'rs1', 'size': 3})
            d = render(c)[0]
            Draft7Validator(schema).validate(d)
            self.assertEqual(d['spec']['tls'], {'mode': 'requireTLS', 'allowInvalidCertificates': False,
                                              'certManagementPolicy': 'userProvidedOnly'})
            self.assertFalse(any(d['spec']['unsafeFlags'].values()))
            self.assertFalse(d['spec']['upgradeOptions']['setFCV'])
            self.assertNotIn('percona.com/delete-psmdb-pvc', d['metadata']['finalizers'])
            self.assertEqual(len(d['spec']['replsets']), 2 if sharded else 1)
            for rs in d['spec']['replsets']:
                conf = yaml.safe_load(rs['configuration'])
                self.assertTrue(conf['security']['enableEncryption'])
                self.assertEqual(rs['affinity']['antiAffinityTopologyKey'], 'kubernetes.io/hostname')
                exporter = rs['sidecars'][0]
                self.assertNotIn('tlsInsecure', ' '.join(exporter['args']))
                self.assertTrue(all('valueFrom' in v for v in exporter['env']))

    def test_backup_schedule_switch_preserves_pbm_and_legacy_default(self):
        c = self.backup_configuration()
        for value in (True, False):
            with self.subTest(enabled=value):
                c['mongodb_backup_schedule_enabled'] = value
                product.validate(c)
                backup = render(c)[0]['spec']['backup']
                self.assertIs(backup['enabled'], True)
                self.assertIs(backup['tasks'][0]['enabled'], value)
                self.assertEqual(backup['tasks'][0]['schedule'], c['mongodb_backup_schedule'])
                self.assertIn('main', backup['storages'])
        del c['mongodb_backup_schedule_enabled']
        product.validate(c)
        self.assertIs(render(c)[0]['spec']['backup']['tasks'][0]['enabled'], True)
        for invalid in (None, 1, 'disabled', []):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                product.validate(c | {'mongodb_backup_schedule_enabled': invalid})

    def test_private_minio_ca_uses_actual_upstream_interface(self):
        self.c.update(mongodb_backup_enabled=True, mongodb_pitr_enabled=True,
                      mongodb_backup_storage_type='minio', mongodb_backup_bucket='mongodb-backup',
                      mongodb_backup_endpoint='backup.mongodb.svc:9000',
                      mongodb_backup_credentials_secret='backup-access', mongodb_backup_ca_secret='backup-ca')
        product.validate(self.c)
        backup = render(self.c)[0]['spec']['backup']
        store = backup['storages']['main']['minio']
        self.assertTrue(store['secure'])
        self.assertFalse(store['insecureSkipTLSVerify'])
        self.assertEqual(store['caBundle'], {'name': 'backup-ca', 'key': 'ca.crt'})
        self.assertTrue(backup['pitr']['enabled'])
        for endpoint in ('http://backup:9000', 'backup:9000/bucket', 'user:password@backup:9000'):
            with self.assertRaises(ValueError):
                product.validate(self.c | {'mongodb_backup_endpoint': endpoint})

    def test_foreign_namespace_and_changed_encryption_identity_are_refused(self):
        api = FakeAPI(self.c)
        result = product.preflight(self.c, api)
        self.assertEqual(len(result['secret_revision']), 64)
        api.foreign = True
        with self.assertRaisesRegex(ValueError, 'namespace'):
            product.preflight(self.c, api)
        api.foreign = False
        api.current = render(self.c)[0]
        with self.assertRaisesRegex(ValueError, 'encryption key'):
            product.preflight(self.c, api)
        api.current['metadata']['annotations']['kubeauto.io/encryption-key-sha256'] = result['encryption_revision']
        self.assertEqual(product.preflight(self.c, api), result)
        api.current['spec']['sharding']['enabled'] = True
        with self.assertRaisesRegex(ValueError, 'topology conversion'):
            product.preflight(self.c, api)

    def test_insufficient_domains_are_refused_before_install(self):
        api = FakeAPI(self.c)
        api.nodes = 2
        with self.assertRaisesRegex(ValueError, 'failure domains'):
            product.preflight(self.c, api)

    def test_preflight_refuses_server_binary_and_operator_downgrades_independent_of_fcv(self):
        for layer in ('server','operator'):
            with self.subTest(layer=layer):
                api=FakeAPI(self.c)
                revision=product.preflight(self.c,api)['encryption_revision']
                api.current=render(self.c|{'mongodb_encryption_revision':revision})[0]
                api.current['status']={'mongoVersion':'8.0.32-14'}
                original=copy.deepcopy(api.current)
                config=self.c|({'mongodb_ver':'8.0.26-11'} if layer=='server' else
                    {'mongodb_operator_ver':'1.23.0','mongodb_operator_chart_ver':'1.23.1'})
                self.assertFalse(api.current['spec']['upgradeOptions']['setFCV'])
                with self.assertRaisesRegex(ValueError,layer+' downgrade is refused'):
                    product.preflight(config,api)
                self.assertEqual(api.current,original)

    def test_preflight_preserves_pinned_version_reapply_and_forward_upgrade(self):
        old=self.c|{'mongodb_ver':'8.0.26-11','mongodb_operator_ver':'1.23.0','mongodb_operator_chart_ver':'1.23.1'}
        api=FakeAPI(old)
        revision=product.preflight(old,api)['encryption_revision']
        api.current=render(old|{'mongodb_encryption_revision':revision})[0]
        product.preflight(old,api)
        product.preflight(self.c,api)

    def test_quorum_health_rejects_multiple_primaries_arbiters_and_missing_members(self):
        status = {'ok': 1, 'members': [{'name': f'n{i}', 'state': 1 if i == 0 else 2, 'health': 1} for i in range(3)]}
        conf = {'members': [{'host': f'n{i}'} for i in range(3)]}
        product.check_members(status, conf, 3)
        status['members'][1]['state'] = 1
        with self.assertRaises(ValueError):
            product.check_members(status, conf, 3)
        status['members'][1]['state'] = 2
        conf['members'][1]['arbiterOnly'] = True
        with self.assertRaises(ValueError):
            product.check_members(status, conf, 3)
        with self.assertRaises(ValueError):
            product.check_members({'ok': 1, 'members': []}, conf, 3)

    def test_crd_change_requires_explicit_request_and_refuses_foreign_consumers(self):
        desired = {'metadata': {'name': 'perconaservermongodbs.psmdb.percona.com'},
                   'spec': {'versions': [{'name': 'v1', 'schema': {'new': True}}]}}
        live = copy.deepcopy(desired)
        live['spec']['versions'][0]['schema'] = {'old': True}

        class CRDAPI:
            consumers = []
            def get(self, *args, **kwargs):
                return live if args[1] == desired['metadata']['name'] else None
            def run(self, *args):
                self.assert_read_only(args)
                return json.dumps({'items': self.consumers})
            @staticmethod
            def assert_read_only(args):
                if args[0] != 'get':
                    raise AssertionError('ownership guard must never mutate')

        api = CRDAPI()
        with self.assertRaisesRegex(ValueError, 'explicit'):
            product.crd_changes(self.c, api, [desired])
        self.c['mongodb_crd_upgrade'] = True
        self.assertEqual(product.crd_changes(self.c, api, [desired])[1], [desired])
        api.consumers = [{'metadata': {'namespace': 'another-customer', 'name': 'foreign'}}]
        with self.assertRaisesRegex(ValueError, 'foreign consumers'):
            product.crd_changes(self.c, api, [desired])
        self.c['mongodb_operator_chart_ver'] = '1.23.1'
        with self.assertRaisesRegex(ValueError, 'rollback is refused'):
            product.crd_changes(self.c, api, [desired])

    def test_health_rejects_stale_controller_generation(self):
        cr = render(self.c)[0]
        cr['metadata']['generation'] = 2
        cr['status'] = {'state': 'ready', 'observedGeneration': 1}
        class StaleAPI:
            def get(self, kind, *args, **kwargs):
                if kind != 'psmdb':
                    raise AssertionError('stale controller status must stop before client execution')
                return cr
        with self.assertRaisesRegex(ValueError, 'current generation'):
            product.health(self.c, StaleAPI())

    def backup_configuration(self):
        self.c.update(mongodb_backup_enabled=True, mongodb_backup_bucket='mongodb-backup',
                      mongodb_backup_endpoint='https://backup.example.org',
                      mongodb_backup_credentials_secret='backup-access')
        return self.c

    def test_backup_and_restore_requests_match_official_schema_including_defaults(self):
        c = self.backup_configuration()
        uid = '12345678-1234-1234-1234-123456789abc'
        prefix = c['mongodb_name'] + '-'
        requests = [
            {'kind': 'backup', 'name': prefix + t, 'type': t} for t in ('logical', 'physical')]
        requests += [
            {'kind': 'restore', 'name': prefix + 'named', 'cluster_uid': uid,
             'backup_name': prefix + 'logical'},
            {'kind': 'restore', 'name': prefix + 'external', 'cluster_uid': uid,
             'destination': 's3://mongodb-backup/prefix/2026-10-15T01:00:00Z', 'type': 'physical'},
            {'kind': 'restore', 'name': prefix + 'pitr', 'cluster_uid': uid,
             'backup_name': prefix + 'logical', 'target_utc': '2026-10-15 02:03:04'}]
        with tarfile.open(ROLE / 'files/psmdb-operator-1.23.2.tgz') as archive:
            crds = list(yaml.safe_load_all(archive.extractfile('psmdb-operator/crds/crd.yaml')))
        for request in requests:
            obj = product.operation_object(c, request)
            if request['kind'] == 'backup':
                self.assertEqual(obj['spec']['compressionType'], 'gzip')
            crd = next(d for d in crds if d['spec']['names']['kind'] == obj['kind'])
            schema = next(v for v in crd['spec']['versions'] if v['name'] == 'v1')['schema']['openAPIV3Schema']
            Draft7Validator(schema).validate(obj)
            spec_schema = schema['properties']['spec']
            self.assertFalse(set(obj['spec']) - set(spec_schema['properties']))
            # Explicit defaults must match Kubernetes defaulting, not a locally
            # guessed subset comparison which could ignore destructive fields.
            for field, definition in spec_schema['properties'].items():
                if 'default' in definition:
                    self.assertEqual(obj['spec'].get(field), definition['default'])

    def test_restore_rejects_ambiguous_sources_paths_dates_and_missing_confirmation(self):
        c = self.backup_configuration()
        request = {'kind': 'restore', 'name': c['mongodb_name'] + '-restore',
                   'cluster_uid': '12345678-1234-1234-1234-123456789abc',
                   'destination': 's3://mongodb-backup/prefix/backup', 'type': 'logical'}
        for change in ({'cluster_uid': ''}, {'backup_name': 'another-backup'},
                       {'destination': 's3://foreign-bucket/backup'},
                       {'destination': 's3://mongodb-backup/a/../backup'},
                       {'destination': 'https://mongodb-backup/backup'},
                       {'target_utc': '2026-02-30 01:02:03'},
                       {'target_utc': '2026-10-15T01:02:03Z'}, {'type': 'incremental'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                product.operation_object(c, request | change)

    def test_request_reuse_requires_exact_spec_ownership_digest_and_restore_target(self):
        c = self.backup_configuration()
        request = {'kind': 'restore', 'name': c['mongodb_name'] + '-restore',
                   'cluster_uid': '12345678-1234-1234-1234-123456789abc',
                   'backup_name': c['mongodb_name'] + '-backup'}
        desired = product.operation_object(c, request)
        cluster = {'metadata': {'labels': product.LABELS, 'uid': request['cluster_uid']}}
        product.check_operation_identity(c, request, desired, copy.deepcopy(desired), cluster)
        for mutation in ('spec', 'label', 'digest', 'target', 'missing-target'):
            existing, target = copy.deepcopy(desired), copy.deepcopy(cluster)
            if mutation == 'spec':
                existing['spec']['selective'] = {'namespaces': ['other.collection']}
            elif mutation == 'label':
                existing['metadata']['labels'] = {}
            elif mutation == 'digest':
                existing['metadata']['annotations'] = {}
            elif mutation == 'target':
                target['metadata']['uid'] = '87654321-1234-1234-1234-123456789abc'
            else:
                target = None
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                product.check_operation_identity(c, request, desired, existing, target)

    def test_operations_create_once_reuse_ready_and_stop_at_first_failed_prerequisite(self):
        c = self.backup_configuration()
        request = {'kind': 'backup', 'name': c['mongodb_name'] + '-backup', 'type': 'logical'}
        c['mongodb_requests'] = [request]
        cluster = {'metadata': {'labels': product.LABELS, 'uid': 'test-cluster'}}

        class OperationsAPI:
            current = None
            creates = 0
            state = 'ready'
            deadline = None
            def get(self, kind, *args, **kwargs):
                return cluster if kind == 'psmdb' else self.current
            def run(self, *args):
                if args[:2] != ('create', '-f'):
                    raise AssertionError('requests must use create, never patch/apply')
                self.creates += 1
                self.current = json.loads(Path(args[2]).read_text())
                self.current['status'] = {'state': self.state, 'completed': '2026-10-15T01:02:03Z',
                                          'destination': 's3://mongodb-backup/backup'}

        api = OperationsAPI()
        with redirect_stdout(StringIO()):
            product.operations(c, api)
            product.operations(c, api)
        self.assertEqual(api.creates, 1)
        for state in ('error', 'rejected'):
            api.current = None
            api.state = state
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, 'operation failed'):
                product.operations(c, api)
        api.current['status'] = {'state': 'ready'}
        with self.assertRaisesRegex(ValueError, 'completion/destination'):
            product.operations(c, api)

    def test_metrics_selector_excludes_routers_without_exporter(self):
        c = self.c | {'mongodb_sharding_enabled': True}
        service = render(c, 'monitoring')[0]
        self.assertEqual(service['spec']['selector']['app.kubernetes.io/component'], 'mongod')

    def test_physical_restore_ready_uses_pbm_identity_without_logical_completion_field(self):
        status = {'state':'ready','pbmName':'2026-10-15T14:50:02.336486329Z'}
        self.assertTrue(product.operation_complete('restore',status))
        self.assertFalse(product.operation_complete('backup',status))
        for invalid in ({'state':'ready'}, status|{'state':'running'}, status|{'error':'failed'},
                        status|{'pbmName':''},status|{'pbmName':None}):
            self.assertFalse(product.operation_complete('restore',invalid))
        backup={'state':'ready','completed':'2026-10-15T14:51:00Z','destination':'s3://bucket/backup'}
        self.assertTrue(product.operation_complete('backup',backup))
        for key in ('completed','destination'):
            self.assertFalse(product.operation_complete('backup',backup|{key:''}))

    def test_existing_physical_restore_ready_finishes_without_recreating_request(self):
        c=self.backup_configuration()
        request={'kind':'restore','name':c['mongodb_name']+'-physical',
                 'cluster_uid':'12345678-1234-1234-1234-123456789abc',
                 'destination':'s3://'+c['mongodb_backup_bucket']+'/backup/snapshot','type':'physical'}
        c['mongodb_requests']=[request]
        cluster={'metadata':{'labels':product.LABELS,'uid':request['cluster_uid']}}
        current=product.operation_object(c,request)
        current['status']={'state':'ready','pbmName':'physical-operation'}
        class API:
            deadline=None
            def get(self,kind,*args,**kwargs):return cluster if kind=='psmdb' else current
            def run(self,*args):raise AssertionError('completed request must not be recreated')
        with redirect_stdout(StringIO()) as output:product.operations(c,API())
        self.assertIn('MONGODB_REQUESTS_CONVERGED count=1',output.getvalue())

    def test_disabling_network_policy_deletes_in_the_configured_namespace(self):
        tasks = yaml.safe_load((ROOT / 'roles/cluster-addon/tasks/mongodb.yml').read_text())
        task = next(t for t in tasks if t.get('register') == 'mongodb_network_delete')
        argv = task['ansible.builtin.command']['argv']
        self.assertEqual(argv[argv.index('--namespace') + 1], '{{ mongodb_namespace }}')
        self.assertIn('{{ mongodb_name }}-access', argv)
        self.assertEqual(task['when'], 'not (mongodb_network_policy_enabled | bool)')

    def test_network_policy_preserves_replication_operator_and_explicit_client_namespace_intersection(self):
        c = self.c | {'mongodb_client_namespaces': ['business']}
        policy = render(c, 'network-policy')[0]
        self.assertEqual(policy['spec']['policyTypes'], ['Ingress'])
        rules = policy['spec']['ingress']
        self.assertEqual(rules[0]['ports'], [{'protocol': 'TCP', 'port': 27017}])
        peers = rules[0]['from']
        self.assertEqual(peers[0]['podSelector']['matchLabels']['app.kubernetes.io/instance'], c['mongodb_name'])
        self.assertEqual(peers[1]['podSelector']['matchLabels']['app.kubernetes.io/instance'], 'mongodb-operator')
        self.assertEqual(peers[2], {'podSelector': {'matchLabels': c['mongodb_client_selector']}})
        self.assertEqual(peers[3], {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'business'}},
                                    'podSelector': {'matchLabels': c['mongodb_client_selector']}})
        for change in ({'mongodb_client_selector': {}}, {'mongodb_client_selector': {'invalid/key/extra': 'x'}},
                       {'mongodb_client_selector': {'valid': True}}, {'mongodb_client_namespaces': ['same', 'same']}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                product.validate(c | change)

    def test_foreign_metric_service_and_network_policy_are_never_adopted(self):
        for foreign in ('service', 'networkpolicy'):
            api = FakeAPI(self.c)
            original = api.get
            def get(kind, *args, **kwargs):
                return {'metadata': {'labels': {}}} if kind == foreign else original(kind, *args, **kwargs)
            api.get = get
            with self.subTest(kind=foreign), self.assertRaisesRegex(ValueError, 'foreign MongoDB ' + foreign):
                product.preflight(self.c, api)

    def test_new_cluster_refuses_existing_same_name_claim_before_operator_can_reuse_it(self):
        api=FakeAPI(self.c);original=api.get
        claim={'metadata':{'name':'mongod-data-'+self.c['mongodb_name']+'-rs0-0',
            'namespace':self.c['mongodb_namespace'],'uid':'foreign-volume',
            'labels':{'app.kubernetes.io/managed-by':'another-product'}},
            'spec':{'storageClassName':self.c['mongodb_storage_class'],'volumeName':'foreign-pv'}}
        def get(kind,*args,**kwargs):
            if kind=='pvc':return {'items':[claim]}
            return original(kind,*args,**kwargs)
        with patch.object(api,'get',side_effect=get),self.assertRaisesRegex(ValueError,'existing.*PVC'):
            product.preflight(self.c,api)

    def test_existing_claims_require_owned_root_labels_storage_and_full_pv_binding(self):
        for drift in (None,'instance','manager','namespace','deleting','storage','pv-storage','pv-namespace','pv-name','pv-uid','unbound','unrelated'):
            with self.subTest(drift=drift):
                api=FakeAPI(self.c);original=api.get
                revision=product.preflight(self.c,api)['encryption_revision']
                api.current=render(self.c|{'mongodb_encryption_revision':revision})[0]
                name='mongod-data-'+self.c['mongodb_name']+'-rs0-0'
                claim={'metadata':{'name':name,'namespace':self.c['mongodb_namespace'],'uid':'claim',
                    'labels':{'app.kubernetes.io/instance':self.c['mongodb_name'],
                        'app.kubernetes.io/managed-by':'percona-server-mongodb-operator'}},
                    'spec':{'storageClassName':self.c['mongodb_storage_class'],'volumeName':'pv'}}
                volume={'spec':{'storageClassName':self.c['mongodb_storage_class'],'claimRef':{
                    'namespace':self.c['mongodb_namespace'],'name':name,'uid':'claim'}}}
                if drift=='instance':claim['metadata']['labels']['app.kubernetes.io/instance']='foreign'
                if drift=='manager':claim['metadata']['labels']['app.kubernetes.io/managed-by']='foreign'
                if drift=='namespace':claim['metadata']['namespace']='foreign'
                if drift=='deleting':claim['metadata']['deletionTimestamp']='2026-10-16T00:00:00Z'
                if drift=='storage':claim['spec']['storageClassName']='foreign'
                if drift=='pv-storage':volume['spec']['storageClassName']='foreign'
                if drift in ('pv-namespace','pv-name','pv-uid'):volume['spec']['claimRef'][drift.removeprefix('pv-')]='foreign'
                if drift=='unbound':claim['spec'].pop('volumeName')
                if drift=='unrelated':claim['metadata']['name']='another-customer-volume'
                def get(kind,*args,**kwargs):
                    if kind=='pvc':return {'items':[claim]}
                    if kind=='pv':return volume
                    return original(kind,*args,**kwargs)
                before=copy.deepcopy((claim,volume))
                with patch.object(api,'get',side_effect=get):
                    if drift not in (None,'unbound','unrelated'):
                        with self.assertRaisesRegex(ValueError,'PVC'):product.preflight(self.c,api)
                    else:product.preflight(self.c,api)
                self.assertEqual((claim,volume),before)
