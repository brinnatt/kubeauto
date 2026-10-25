"""Deterministic LONGHORN-37 product, credential, recovery and cleanup contracts."""
import base64
import contextlib
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v1_middleware_contract', ROOT/'tests/helpers/longhorn-v1-middleware.py')
GATE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(GATE)


def native_bindings():
    return {'datadir-'+GATE.CLUSTER+'-pxc-'+str(i): {'claim_uid': 'claim-'+str(i), 'pv': 'pv-'+str(i),
        'pv_uid': 'pv-uid-'+str(i), 'volume': 'vol-'+str(i), 'volume_uid': 'volume-'+str(i),
        'engine_uid': 'engine-'+str(i), 'replicas': {str(j): {'uid': 'replica-'+str(i)+'-'+str(j),
            'node': 'longhorn-node-'+n} for j,n in enumerate(('212','165','238'))}} for i in range(3)}


def row_fixture():
    return {'run_id': 'unit-run', 'cluster_uid': 'cluster', 'namespace_uid': 'longhorn',
        'database_namespace': 'owned-db', 'operator_namespace': 'owned-operator',
        'database_namespace_uid': 'database-ns', 'operator_namespace_uid': 'operator-ns',
        'pxc_uid': 'database', 'rendered_sha256': 'a'*64, 'bindings': native_bindings(),
        'monitoring_identity': {'monitor_namespace_uid':'monitor-ns','storage_class_uid':'monitor-sc',
            'claims':{str(i):{'uid':'m-'+str(i)} for i in range(6)}}}


def database_fixture(directory):
    row = row_fixture(); tables = {}; trace = []
    def write(path, value): path.write_text(json.dumps(value)); path.chmod(0o600)
    pod = {'metadata': {'uid': 'old-pod', 'name': GATE.CLUSTER+'-pxc-1',
        'ownerReferences': [{'kind': 'StatefulSet', 'uid': 'sts', 'controller': True}]},
        'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
    current = copy.deepcopy(pod)
    def get(kind, *args, **kwargs):
        if kind == 'pod': return copy.deepcopy(current)
        if kind == 'statefulset': return {'metadata': {'uid': 'sts'}}
        raise AssertionError(kind)
    lab = SimpleNamespace(DIRECTORY=Path(directory), write_private=write, get=Mock(side_effect=get),
        remove=Mock(side_effect=lambda obj: current['metadata'].update(uid='new-pod')),
        wait=lambda label, timeout, fn: fn())
    def execute(query):
        trace.append(query)
        if query.startswith('SELECT SCHEMA_NAME'): return ''
        if query.startswith('SELECT id,HEX(payload)'):
            schema = re.search(r'FROM ([a-z0-9_]+)\.marker', query)[1]
            return '\n'.join(str(i)+'\t'+v for i,v in sorted(tables[schema].items()))
        for schema in re.findall(r'CREATE DATABASE ([a-z0-9_]+)', query): tables[schema] = {}
        match = re.search(r'INSERT INTO ([a-z0-9_]+)\.marker VALUES (.*?);', query)
        if match:
            tables[match[1]].update({int(i):v for i,v in re.findall(r"\((\d+),UNHEX\('([A-F0-9]+)'\)\)", match[2])})
        return ''
    def sql(lab_arg, row_arg, query, pod=None): return execute(query)
    def client(lab_arg, row_arg, args, data='', pod=None):
        trace.append(('client', list(args), pod))
        if args[0] == 'mysqldump':
            schema = args[-2]
            header = 'CREATE TABLE marker(id INT PRIMARY KEY,payload VARBINARY(32)) ENGINE=InnoDB;\nINSERT INTO marker VALUES '
            if '--hex-blob' not in args:
                # MySQL 8.4 emits binary column values unless hex notation is
                # requested. Preserve strict decoding at the real text boundary.
                raw = header.encode() + b','.join(b"("+str(i).encode()+b",'"+bytes.fromhex(v)+b"')" for i,v in sorted(tables[schema].items()))+b';\n'
                return raw.decode('utf-8')
            return header+','.join('('+str(i)+',0x'+v+')' for i,v in sorted(tables[schema].items()))+';\n'
        if data.startswith('CREATE TABLE'):
            tables[args[-1]].update({int(i):v for i,v in re.findall(r"\((\d+),0x([A-F0-9]+)\)", data)})
            return ''
        if 'SHOW GLOBAL STATUS' in data: return '1\nwsrep_cluster_size\t3\nwsrep_cluster_status\tPrimary\nwsrep_local_state_comment\tSynced\n'
        return execute(data)
    monitor = SimpleNamespace(verify=Mock(), CLASS='longhorn-monitoring-one-replica',
        receipt=lambda l: copy.deepcopy(row['monitoring_identity']))
    return row, lab, monitor, sql, client, trace


def business_log():
    with tempfile.TemporaryDirectory() as directory:
        row, lab, monitor, sql, client, trace = database_fixture(directory); output = io.StringIO()
        with patch.object(GATE, 'receipt', return_value=row), patch.object(GATE, 'monitor', return_value=monitor), \
                patch.object(GATE, 'pxc', return_value={'status': {'state': 'ready'}}), \
                patch.object(GATE, 'bindings', return_value=native_bindings()), patch.object(GATE, 'sql', side_effect=sql), \
                patch.object(GATE, 'client', side_effect=client), contextlib.redirect_stdout(output):
            GATE.business(lab, 123)
        return output.getvalue()


class MiddlewareContracts(unittest.TestCase):
    def test_native_varbinary_dump_uses_lossless_text_and_restores_independent_schema(self):
        # Exercise the complete owning business path with the native binary
        # fixture. Missing --hex-blob raises at UTF-8 capture before backup.
        text = business_log()
        GATE.verify_log(text)
        proof = next(json.loads(line.split(' ', 1)[1]) for line in text.splitlines()
                     if line.startswith('LONGHORN_MIDDLEWARE_NATIVE_LOGICAL_RECOVERY '))
        self.assertEqual(proof['source'], proof['restored'])
        self.assertEqual(proof['source']['rows'], 1024)
        self.assertTrue(proof['isolation_verified'])

    def default_monitoring_input(self):
        defaults = yaml.safe_load((ROOT/'roles/longhorn/defaults/main.yml').read_text())
        self.assertEqual(['v1'], defaults['longhorn_data_engines'])
        source = yaml.safe_load((ROOT/'conf/config.yml').read_text())
        source.pop('longhorn_data_engines', None)
        source.update(mysql_install='no', prom_install='no', longhorn_monitoring_enabled=False,
                      longhorn_requests=[])
        monitor = GATE.monitor()
        return monitor, monitor.desired(yaml.safe_dump(source), 'owned-monitor')

    def test_actual_monitoring_to_database_handoff_consumes_omitted_v1_default(self):
        monitor, ready = self.default_monitoring_input()
        self.assertNotIn('longhorn_data_engines', ready)
        before = copy.deepcopy(ready)
        config = GATE.desired(yaml.safe_dump(ready), 'owned-db', 'owned-operator')
        self.assertEqual(before, ready)
        self.assertEqual('v1', config['longhorn_storage_classes'][-1]['data_engine'])
        environment = monitor.jinja2.Environment(undefined=monitor.jinja2.StrictUndefined)
        environment.filters.update(bool=bool, quote=json.dumps)
        text = environment.from_string(
            (ROOT/'roles/cluster-addon/templates/percona-pxc/cluster.yaml.j2').read_text()).render(**config)
        obj = yaml.safe_load(text)
        self.assertEqual(GATE.CLASS, obj['spec']['pxc']['volumeSpec']['persistentVolumeClaim']['storageClassName'])
        self.assertEqual(3, obj['spec']['pxc']['size'])
        self.assertEqual(3, obj['spec']['haproxy']['size'])

    def test_null_empty_mixed_unknown_and_v2_are_not_v1_defaults(self):
        _, ready = self.default_monitoring_input()
        for value in (None, [], ['v2'], ['v1', 'v2'], ['unknown'], False):
            invalid = copy.deepcopy(ready)
            invalid['longhorn_data_engines'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                GATE.desired(yaml.safe_dump(invalid), 'owned-db', 'owned-operator')

    def test_database_handoff_selects_only_database_addon_and_preserves_monitoring_reference(self):
        _, ready = self.default_monitoring_input()
        selected = GATE.desired(yaml.safe_dump(ready), 'owned-db', 'owned-operator')
        tasks = yaml.safe_load((ROOT/'roles/cluster-addon/tasks/main.yml').read_text())
        env = GATE.monitor().jinja2.Environment()
        for name, expected in (('prometheus.yml', False), ('percona-pxc.yml', True)):
            task = next(t for t in tasks if t.get('import_tasks') == name)
            self.assertEqual(expected, bool(env.compile_expression(task['when'])(**selected)))
        self.assertTrue(selected['longhorn_monitoring_enabled'])
        for key in ('prom_namespace', 'prom_storage_class', 'longhorn_monitoring_namespace',
                    'longhorn_monitoring_selector', 'longhorn_monitoring_release'):
            self.assertEqual(ready[key], selected[key])
        self.assertEqual('yes', ready['prom_install'])

    def test_v2_handoff_still_requires_an_explicit_v2_engine(self):
        _, ready = self.default_monitoring_input()
        v2 = GATE.load('longhorn-v2-middleware.py').SHARED
        with self.assertRaises(ValueError):
            v2.desired(yaml.safe_dump(ready), 'owned-db', 'owned-operator')
        ready['longhorn_data_engines'] = ['v2']
        config = v2.desired(yaml.safe_dump(ready), 'owned-db', 'owned-operator')
        self.assertEqual('v2', config['longhorn_storage_classes'][-1]['data_engine'])

    def test_product_config_declares_separate_three_replica_v1_delete_class_and_explicit_database_class(self):
        original = yaml.safe_load((ROOT/'conf/config.yml').read_text())
        original.update(mysql_install='no', longhorn_data_engines=['v1'], prom_install='yes', longhorn_monitoring_enabled=True)
        original_classes = copy.deepcopy(original['longhorn_storage_classes'])
        config = GATE.desired(yaml.safe_dump(original), 'owned', 'operator')
        self.assertEqual(original_classes, config['longhorn_storage_classes'][:-1])
        fixture = config['longhorn_storage_classes'][-1]
        self.assertEqual((GATE.CLASS, 3, 'v1', 'Delete'), (fixture['name'],fixture['replicas'],fixture['data_engine'],fixture['reclaim_policy']))
        self.assertEqual((3,3,'2Gi',GATE.CLASS), (config['mysql_pxc_size'],config['mysql_haproxy_size'],config['mysql_pvc_size'],config['mysql_storage_class']))
        self.assertTrue(config['mysql_tls_enabled']); self.assertFalse(config['mysql_backup_enabled']); self.assertFalse(config['mysql_pitr_enabled'])
        self.assertEqual('2Gi', config['mysql_pxc_memory_request'])
        for key, value in (('mysql_install','yes'), ('longhorn_data_engines',['v2']), ('prom_install','no'), ('longhorn_requests',[{}])):
            invalid = copy.deepcopy(original); invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): GATE.desired(yaml.safe_dump(invalid), 'owned', 'operator')

    def test_actual_database_template_renders_fixed_versions_class_tls_and_bounded_resources(self):
        module = GATE.monitor(); source = yaml.safe_load((ROOT/'conf/config.yml').read_text())
        source.update(mysql_install='no', longhorn_data_engines=['v1'], prom_install='yes', longhorn_monitoring_enabled=True)
        config = GATE.desired(yaml.safe_dump(source), 'owned', 'operator')
        environment = module.jinja2.Environment(undefined=module.jinja2.StrictUndefined)
        environment.filters.update(bool=lambda v: bool(v), quote=lambda v: json.dumps(v))
        text = environment.from_string((ROOT/'roles/cluster-addon/templates/percona-pxc/cluster.yaml.j2').read_text()).render(**config)
        obj = yaml.safe_load(text); spec = obj['spec']
        self.assertEqual('1.20.0', str(spec['crVersion']))
        self.assertTrue(spec['pxc']['image'].endswith(':8.4.8-8.1'))
        self.assertEqual(GATE.CLASS, spec['pxc']['volumeSpec']['persistentVolumeClaim']['storageClassName'])
        self.assertEqual('2Gi', spec['pxc']['resources']['requests']['memory'])
        self.assertTrue(spec['tls']['enabled']); self.assertFalse(spec['unsafeFlags']['pxcSize'])
        self.assertEqual(7, len(GATE.planned(config)))
        self.assertEqual(3, sum(r['group']=='pxc' for r in GATE.planned(config)))

    def test_cache_reuses_five_exact_release_records_and_restores_publication_provider_on_failure(self):
        original = Mock(return_value=([{'key':'base'}], [{'key':'support'}]))
        lab = SimpleNamespace(BASE=ROOT, publication_records=original)
        def fail(**kwargs):
            production, support = lab.publication_records()
            self.assertEqual(6, len(production)); self.assertEqual([{'key':'support'}], support)
            self.assertEqual(5, len(kwargs['keys'])); raise ValueError('cache fault')
        lab.cache_artifacts = Mock(side_effect=fail)
        with self.assertRaisesRegex(ValueError, 'cache fault'): GATE.cache(lab)
        self.assertIs(original, lab.publication_records)

    def test_runtime_admission_includes_existing_monitoring_pods_and_host_memory_and_actual_disk_threshold(self):
        config = yaml.safe_load((ROOT/'conf/config.yml').read_text()); config.update(longhorn_data_engines=['v1'], prom_install='yes', longhorn_monitoring_enabled=True)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'config.yml').write_text(yaml.safe_dump(config))
            module = GATE.monitor(); module.verify = Mock(); module.host_memory = Mock(return_value={'a': {'MemAvailable': 6*1024**3}})
            module.placement = Mock(return_value={'placement':'proof'})
            existing = [{'metadata': {'name':'prometheus'},'spec':{'containers':[{'resources':{'requests':{'memory':'4Gi'}}}]}}]
            lab = SimpleNamespace(DIRECTORY=Path(directory), WORKERS=('a',), get=Mock(side_effect=lambda kind,*a,**kw:
                {'items':existing} if kind=='pods' else {'items':[]} if kind=='nodes' else {'value':'25'}))
            disks = SimpleNamespace(filesystem_rows=lambda l: {'n': {'available': 90*1024**3,'maximum':100*1024**3,'reserved':0,'scheduled':0}})
            with patch.object(GATE,'monitor',return_value=module), patch.object(GATE,'load',return_value=disks):
                GATE.admission(lab,1)
                self.assertIs(existing,module.placement.call_args.args[1])
                module.host_memory.return_value['a']['MemAvailable'] = 4*1024**3
                with self.assertRaisesRegex(ValueError,'five GiB'): GATE.admission(lab,1)

    def test_exec_framing_transmits_password_only_in_stdin_and_never_argv_or_log(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); fake=base/'mysql'
            fake.write_text('#!/bin/sh\n[ "$MYSQL_PWD" = unit-private-password ] || exit 7\ncat >/dev/null\nprintf auth-ready\n'); fake.chmod(0o755)
            secret={'metadata':{'uid':'secret'},'data':{'root':base64.b64encode(b'unit-private-password').decode()}}
            calls=[]
            def kubectl(*args,**kwargs):
                calls.append((args,kwargs)); argv=list(args[args.index('--')+1:])
                result=subprocess.run(argv,input=kwargs['stdin'],text=True,capture_output=True,check=True,
                    env=dict(os.environ,PATH=str(base)+':'+os.environ['PATH']))
                return SimpleNamespace(stdout=result.stdout)
            lab=SimpleNamespace(DIRECTORY=base,get=Mock(return_value=secret),write_private=Mock(),kubectl=kubectl)
            row=row_fixture(); output=io.StringIO()
            with contextlib.redirect_stdout(output): result=GATE.client(lab,row,['mysql'],'SELECT 1;')
            self.assertEqual('auth-ready',result); self.assertNotIn('unit-private-password',str(calls[0][0])); self.assertFalse(output.getvalue())
            self.assertIn('--init-command=SET SESSION wsrep_sync_wait=1',calls[0][0])
            secret['metadata']['uid']='replacement'
            with self.assertRaisesRegex(ValueError,'credential identity'): GATE.credential(lab,row)

    def test_native_backup_file_requires_private_mode_original_inode_and_verified_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'backup.sql'; p.write_bytes(b'owned-backup');p.chmod(0o600);info=p.stat()
            row={'logical_backup':{'name':p.name,'device':info.st_dev,'inode':info.st_ino,'bytes':info.st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}}
            lab=SimpleNamespace(DIRECTORY=Path(directory));self.assertEqual(p,GATE.backup_file(lab,row))
            p.chmod(0o644)
            with self.assertRaises(ValueError):GATE.backup_file(lab,row)
            p.chmod(0o600);p.write_bytes(b'other-backup')
            with self.assertRaises(ValueError):GATE.backup_file(lab,row)

    def test_native_business_dump_restore_write_isolation_and_local_three_member_persistence_execute(self):
        text=business_log();GATE.verify_log(text)
        recovery=json.loads(next(l.split(' ',1)[1] for l in text.splitlines() if l.startswith('LONGHORN_MIDDLEWARE_NATIVE_LOGICAL_RECOVERY ')))
        self.assertEqual(1024,recovery['source']['rows']);self.assertEqual(recovery['source'],recovery['restored'])
        self.assertTrue(recovery['isolation_verified']);self.assertGreater(recovery['backup']['bytes'],100)
        self.assertNotIn('password',text)

    def test_raw_marker_only_changed_pv_missing_member_false_isolation_and_same_pod_never_pass(self):
        full=business_log()
        for mutation in ('marker','identity','member','isolation','restart','hash','failure'):
            lines=full.splitlines()
            if mutation=='marker':lines=['LONGHORN_MIDDLEWARE_BUSINESS_PASS']
            if mutation=='identity':lines=[l.replace('"pv_uid": "pv-uid-0"','"pv_uid": "foreign"') if l.startswith('LONGHORN_MIDDLEWARE_MEMBER_PERSISTENCE ') else l for l in lines]
            if mutation=='member':lines=[l.replace('"longhorn-pxc-pxc-2":','"foreign":') for l in lines]
            if mutation=='isolation':lines=[l.replace('"isolation_verified": true','"isolation_verified": false') for l in lines]
            if mutation=='restart':lines=[l.replace('"replacement_uid": "new-pod"','"replacement_uid": "old-pod"') for l in lines]
            if mutation=='hash':lines=[l.replace('"sha256": "','"sha256": "invalid') for l in lines]
            if mutation=='failure':lines.append('LONGHORN_FIRST_FAILURE phase=unit')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):GATE.verify_log('\n'.join(lines))

    def graph_fixture(self):
        row=row_fixture();ns={'metadata':{'uid':'database-ns','labels':{'kubeauto.io/longhorn-middleware-run':'unit-run'}}}
        secret={'kind':'Secret','metadata':{'uid':'secret','name':GATE.CLUSTER+'-secrets','labels':{'app.kubernetes.io/instance':GATE.CLUSTER}}}
        claim={'kind':'PersistentVolumeClaim','metadata':{'uid':'claim','name':'datadir','labels':{'app.kubernetes.io/instance':GATE.CLUSTER}},'spec':{'storageClassName':GATE.CLASS}}
        lab=SimpleNamespace(DIRECTORY=Path('/unused'),write_private=Mock(),kubectl=Mock(return_value=SimpleNamespace(stdout='secrets pvcs')),
            get=Mock(side_effect=lambda kind,*args,**kw:ns if kind=='namespace' else {'items':[secret]} if kind=='secrets' else {'items':[claim]}))
        return lab,row,ns,secret,claim

    def test_graph_uses_official_unowned_system_secret_labels_then_freezes_uid_and_rejects_foreign_claim(self):
        lab,row,ns,secret,claim=self.graph_fixture();self.assertEqual(2,len(GATE.graph(lab,row,'database_namespace')))
        secret['metadata']['uid']='replacement'
        with self.assertRaisesRegex(ValueError,'Secret was replaced'):GATE.graph(lab,row,'database_namespace')
        secret['metadata']['uid']='secret';claim['metadata']['uid']='foreign';claim['spec']['storageClassName']='customer'
        with self.assertRaisesRegex(ValueError,'foreign object'):GATE.graph(lab,row,'database_namespace')
        ns['metadata']['uid']='replacement'
        with self.assertRaisesRegex(ValueError,'namespace ownership'):GATE.graph(lab,row,'database_namespace')

    def test_official_internal_secret_copy_binds_actual_cr_source_data_and_original_uid(self):
        lab,row,ns,secret,claim=self.graph_fixture()
        source = copy.deepcopy(secret); source.update(type='Opaque', data={'root':'unit-canary'})
        secret.update(type='Opaque',data=copy.deepcopy(source['data']))
        secret['metadata'].update(name='internal-'+GATE.CLUSTER, labels={})
        cr = {'metadata':{'uid':row['pxc_uid']},'spec':{'secretsName':GATE.CLUSTER+'-secrets',
            'pxc':{'size':3,'volumeSpec':{'persistentVolumeClaim':{'storageClassName':GATE.CLASS}}},'haproxy':{'size':3}}}
        lab.get = Mock(side_effect=lambda kind,*a,**kw: ns if kind=='namespace' else source if kind=='secret' else
            cr if kind=='perconaxtradbclusters.pxc.percona.com' else {'items':[secret]} if kind=='secrets' else {'items':[claim]})
        GATE.graph(lab,row,'database_namespace')
        for defect in ('uid','data','type','cr','reference'):
            saved = copy.deepcopy((secret,cr))
            if defect=='uid':secret['metadata']['uid']='replacement'
            if defect=='data':secret['data']['root']='foreign'
            if defect=='type':secret['type']='kubernetes.io/tls'
            if defect=='cr':cr['metadata']['uid']='foreign'
            if defect=='reference':cr['spec']['secretsName']='foreign'
            with self.subTest(defect=defect),self.assertRaises(ValueError):GATE.graph(lab,row,'database_namespace')
            secret.clear();secret.update(saved[0]);cr.clear();cr.update(saved[1])
        # Native CR deletion precedes the final namespace graph. Only the
        # journaled source/internal pair may survive that handoff.
        cr.clear()
        GATE.graph(lab,row,'database_namespace')
        source['metadata']['uid']='replacement-source'
        with self.assertRaises(ValueError):GATE.graph(lab,row,'database_namespace')

    def test_unlabelled_webhook_secret_requires_chart_controller_exact_keys_and_original_uid(self):
        row=row_fixture();ns={'metadata':{'uid':row['operator_namespace_uid'],
            'labels':{'kubeauto.io/longhorn-middleware-run':row['run_id']}}}
        deployment={'kind':'Deployment','metadata':{'uid':'deploy','name':'pxc-operator','annotations':{
            'meta.helm.sh/release-name':'pxc-operator','meta.helm.sh/release-namespace':row['operator_namespace']}}}
        secret={'kind':'Secret','metadata':{'uid':'webhook','name':'pxc-webhook-ssl'},
            'type':'Opaque','data':dict.fromkeys(('ca.crt','tls.crt','tls.key'),'unit-canary')}
        objects=[secret,deployment]
        lab=SimpleNamespace(DIRECTORY=Path('/unused'),write_private=Mock(),
            kubectl=Mock(return_value=SimpleNamespace(stdout='objects')),
            get=Mock(side_effect=lambda kind,*a,**kw:ns if kind=='namespace' else {'items':objects}))
        GATE.graph(lab,row,'operator_namespace')
        for defect in ('uid','keys','type','owner','controller'):
            saved=copy.deepcopy((secret,deployment))
            if defect=='uid':secret['metadata']['uid']='replacement'
            if defect=='keys':secret['data'].pop('ca.crt')
            if defect=='type':secret['type']='kubernetes.io/tls'
            if defect=='owner':secret['metadata']['ownerReferences']=[{'uid':'foreign'}]
            if defect=='controller':deployment['metadata']['annotations']['meta.helm.sh/release-namespace']='foreign'
            with self.subTest(defect=defect),self.assertRaises(ValueError):GATE.graph(lab,row,'operator_namespace')
            secret.clear();secret.update(saved[0]);deployment.clear();deployment.update(saved[1])
        objects.remove(deployment)
        GATE.graph(lab,row,'operator_namespace')
        secret['metadata']['uid']='replacement-after-uninstall'
        with self.assertRaises(ValueError):GATE.graph(lab,row,'operator_namespace')

    def test_operator_leader_lease_requires_owned_pod_identity_and_freezes_lease_uid(self):
        row=row_fixture();ns=row['operator_namespace']
        pod={'kind':'Pod','metadata':{'uid':'pod','name':'operator-pod','namespace':ns,
            'labels':{'app.kubernetes.io/name':'pxc-operator'}}}
        obj={'kind':'Lease','metadata':{'uid':'lease','name':'08db1feb.percona.com','namespace':ns},
            'spec':{'holderIdentity':'operator-pod_12345678-1234-1234-1234-123456789abc'}}
        self.assertTrue(GATE.operator_lease(obj,{'pod':pod},{'pod'},row))
        row['operator_lease_uid']='lease'
        for defect in ('uid','name','namespace','holder','pod','owner','label'):
            lease=copy.deepcopy(obj);current=copy.deepcopy(pod);allowed={'pod'}
            if defect=='uid':lease['metadata']['uid']='replacement'
            if defect=='name':lease['metadata']['name']='foreign'
            if defect=='namespace':lease['metadata']['namespace']='foreign'
            if defect=='holder':lease['spec']['holderIdentity']='operator-pod_not-a-uuid'
            if defect=='pod':lease['spec']['holderIdentity']='foreign-pod_12345678-1234-1234-1234-123456789abc'
            if defect=='owner':allowed=set()
            if defect=='label':current['metadata']['labels']={}
            with self.subTest(defect=defect):
                if defect=='uid':
                    with self.assertRaisesRegex(ValueError,'Lease was replaced'):GATE.operator_lease(lease,{'pod':current},allowed,row)
                else:self.assertFalse(GATE.operator_lease(lease,{'pod':current},allowed,row))

    def test_operator_graph_accepts_typed_endpoint_and_lease_events_after_controller_ancestry(self):
        row=row_fixture();ns=row['operator_namespace']
        def obj(kind,name,uid,**fields):
            return dict(kind=kind,metadata=dict(name=name,uid=uid,namespace=ns),**fields)
        deployment=obj('Deployment','pxc-operator','deployment')
        deployment['metadata']['annotations']={'meta.helm.sh/release-name':'pxc-operator','meta.helm.sh/release-namespace':ns}
        pod=obj('Pod','operator-pod','pod')
        pod['metadata'].update(ownerReferences=[{'uid':'deployment'}],labels={'app.kubernetes.io/name':'pxc-operator'})
        service=obj('Service','operator-service','service',spec={'selector':{'app.kubernetes.io/name':'pxc-operator'}})
        service['metadata'].update(ownerReferences=[{'uid':'deployment'}],labels={'name':'operator'})
        endpoint=obj('Endpoints','operator-service','endpoint',subsets=[{'addresses':[{'targetRef':{
            'kind':'Pod','name':'operator-pod','uid':'pod','namespace':ns}}]}])
        endpoint['metadata']['labels']={'name':'operator','endpoints.kubernetes.io/managed-by':'endpoint-controller'}
        lease=obj('Lease','08db1feb.percona.com','lease',spec={'holderIdentity':'operator-pod_12345678-1234-1234-1234-123456789abc'})
        event=obj('Event','leader-election','event',involvedObject={'uid':'lease'})
        objects=[event,lease,endpoint,service,pod,deployment]
        namespace={'metadata':{'uid':row['operator_namespace_uid'],'labels':{'kubeauto.io/longhorn-middleware-run':row['run_id']}}}
        lab=SimpleNamespace(DIRECTORY=Path('/unused'),write_private=Mock(),kubectl=Mock(return_value=SimpleNamespace(stdout='objects')),
            get=Mock(side_effect=lambda kind,*a,**kw:namespace if kind=='namespace' else {'items':objects}))
        self.assertEqual(6,len(GATE.graph(lab,row,'operator_namespace')))
        self.assertEqual('lease',row['operator_lease_uid'])
        row.pop('known_object_uids');endpoint['subsets'][0]['addresses'][0]['targetRef']['uid']='foreign'
        with self.assertRaisesRegex(ValueError,'foreign object'):GATE.graph(lab,row,'operator_namespace')

    def test_foreign_and_symlink_receipt_prevent_any_cleanup_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/GATE.RECORD;path.write_text('{}')
            lab=SimpleNamespace(DIRECTORY=Path(directory),owner=lambda:{'run_id':'run','cluster_uid':'cluster','namespace_uid':'namespace'},remove=Mock())
            with self.assertRaises(ValueError):GATE.cleanup(lab,123)
            path.unlink();path.symlink_to(Path(directory)/'foreign')
            with self.assertRaises(ValueError):GATE.cleanup(lab,123)
            lab.remove.assert_not_called()

    def test_namespace_foreign_graph_blocks_native_database_and_operator_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            row=row_fixture();(Path(directory)/GATE.RECORD).write_text('{}');(Path(directory)/'config.yml').write_text('owned')
            row['allowed_inputs']=['owned'];lab=SimpleNamespace(DIRECTORY=Path(directory),remove=Mock(),execute=Mock())
            with patch.object(GATE,'receipt',return_value=row),patch.object(GATE,'namespace_identity',return_value={}),patch.object(GATE,'graph',side_effect=ValueError('foreign graph')):
                # A real namespace is truthy. Use a UID-bearing object so the
                # refusal happens before pxc/helm/consumer mutations.
                with patch.object(GATE,'namespace_identity',return_value={'metadata':{'uid':'ns'}}):
                    with self.assertRaisesRegex(ValueError,'foreign graph'):GATE.cleanup(lab,123)
            lab.remove.assert_not_called();lab.execute.assert_not_called()

    def test_real_binding_chain_requires_v1_three_original_filesystem_disks_and_two_gib(self):
        saved=native_bindings();row=row_fixture();claims=[];pvs={};volumes={};engines=[];replicas=[]
        disks={'longhorn-node-'+n:{'disk_uuid':'disk-'+n} for n in ('212','165','238')}
        for name,b in saved.items():
            claims.append({'metadata':{'name':name,'uid':b['claim_uid']},'spec':{'volumeName':b['pv'],
                'storageClassName':GATE.CLASS,'resources':{'requests':{'storage':'2Gi'}}},'status':{'phase':'Bound'}})
            pvs[b['pv']]={'metadata':{'name':b['pv'],'uid':b['pv_uid']},'spec':{'claimRef':{'uid':b['claim_uid']},
                'storageClassName':GATE.CLASS,'persistentVolumeReclaimPolicy':'Delete','capacity':{'storage':'2Gi'},
                'csi':{'driver':'driver.longhorn.io','volumeHandle':b['volume']}}}
            volumes[b['volume']]={'metadata':{'uid':b['volume_uid']},'spec':{'dataEngine':'v1','numberOfReplicas':3,'size':str(2*1024**3)}}
            owner=[{'kind':'Volume','uid':b['volume_uid']}]
            engines.append({'metadata':{'uid':b['engine_uid'],'ownerReferences':owner},'spec':{'volumeName':b['volume'],'dataEngine':'v1'}})
            replicas += [{'metadata':{'uid':r['uid'],'ownerReferences':copy.deepcopy(owner)},
                'spec':{'volumeName':b['volume'],'dataEngine':'v1','nodeID':r['node'],'diskID':disks[r['node']]['disk_uuid']}}
                for r in b['replicas'].values()]
        def get(kind,name=None,*args,**kw):
            return {'items':claims} if kind=='pvc' else pvs[name] if kind=='pv' else volumes[name] if kind=='volumes.longhorn.io' else {'items':engines} if kind=='engines.longhorn.io' else {'items':replicas}
        lab=SimpleNamespace(get=Mock(side_effect=get),replica_topology=lambda b,n:next(r['replicas'] for r in saved.values() if r['volume']==b['volume']))
        common=SimpleNamespace(filesystem_rows=lambda l:disks)
        module=GATE.monitor()
        with patch.object(GATE,'load',return_value=common),patch.object(GATE,'monitor',return_value=module):
            self.assertEqual(saved,GATE.bindings(lab,row))
            for mutation in ('disk','owner','engine','capacity','pv'):
                backup=copy.deepcopy((replicas,engines,volumes,pvs))
                if mutation=='disk':replicas[0]['spec']['diskID']='foreign'
                if mutation=='owner':replicas[0]['metadata']['ownerReferences'][0]['uid']='foreign'
                if mutation=='engine':engines[0]['spec']['dataEngine']='v2'
                if mutation=='capacity':next(iter(volumes.values()))['spec']['size']='1'
                if mutation=='pv':next(iter(pvs.values()))['metadata']['uid']='replacement'
                with self.subTest(mutation=mutation),self.assertRaises(ValueError):GATE.bindings(lab,row)
                replicas[:],engines[:],restored_volumes,restored_pvs=backup
                volumes.clear();volumes.update(restored_volumes);pvs.clear();pvs.update(restored_pvs)

    def test_monitor_namespace_class_and_six_claim_identities_cannot_be_rebound(self):
        row=row_fixture();current=copy.deepcopy(row['monitoring_identity'])
        with patch.object(GATE,'monitor',return_value=SimpleNamespace(receipt=lambda lab:current)):
            self.assertEqual(current,GATE.monitor_identity(None,row))
            current['claims']['0']['uid']='replacement'
            with self.assertRaisesRegex(ValueError,'monitoring namespace/class/PVC/PV'):GATE.monitor_identity(None,row)

    def test_reentry_qualification_matches_archived_run_before_real_namespace_graph_persistence(self):
        from tests.unit.test_longhorn_v2_monitoring import NamespaceGraphContracts, GATE as monitoring
        fixture=NamespaceGraphContracts();lab,mon,objects,release=fixture.fixture()
        release.update(info={'status':'superseded'},config={'owned':'values'})
        fixture.encode_release(objects[5],release)
        second=copy.deepcopy(objects[5]);second['metadata'].update(uid='helm-2',name='sh.helm.release.v1.prometheus.v2')
        newer=copy.deepcopy(release);newer.update(version=2,info={'status':'deployed'})
        fixture.encode_release(second,newer);objects.append(second)
        row=row_fixture();row.update(original_input='monitor-ready',allowed_inputs=['monitor-ready','database-input'])
        mon.update({k:row[k] for k in ('run_id','cluster_uid','namespace_uid')},ready_inputs='monitor-ready')
        ns=lab.get('namespace','owned');ns['metadata']['labels']['kubeauto.io/longhorn-monitoring-run']=row['run_id']
        proof={k:row[k] for k in ('run_id','cluster_uid','namespace_uid','database_namespace','database_namespace_uid',
            'operator_namespace','operator_namespace_uid')}
        proof.update(monitor_namespace=mon['namespace'],monitor_namespace_uid=mon['monitor_namespace_uid'],release_history=[])
        for obj in (objects[5],second):
            value=json.loads(gzip.decompress(base64.b64decode(base64.b64decode(obj['data']['release']))))
            proof['release_history'].append(dict(name=obj['metadata']['name'],uid=obj['metadata']['uid'],
                secret_release_sha256=hashlib.sha256(obj['data']['release'].encode()).hexdigest(),
                revision=value['version'],status=value['info']['status'],chart=value['chart']['metadata']['name'],
                chart_version=value['chart']['metadata']['version'],
                config_sha256=hashlib.sha256(json.dumps(value['config'],sort_keys=True).encode()).hexdigest(),
                hook_names=[h['name'] for h in value['hooks']]))
        original_get=lab.get
        lab.get=Mock(side_effect=lambda kind,*a,**kw:ns if kind=='namespace' else {'items':objects})
        with tempfile.TemporaryDirectory() as directory:
            lab.BASE=lab.DIRECTORY=Path(directory)
            path=lab.BASE/'tests/evidence/longhorn-middleware-reentry-first-failure-20261022.json';path.parent.mkdir(parents=True)
            (lab.DIRECTORY/'config.yml').write_text('database-input')
            def save():path.write_text(json.dumps({'reentry_proof':proof}))
            save()
            with patch.object(GATE,'receipt',return_value=row),patch.object(GATE,'monitor',return_value=monitoring), \
                    patch.object(GATE,'namespace_identity',return_value={'owned':True}),patch.object(monitoring,'receipt',return_value=mon):
                GATE.qualify_reentry(lab,123)
                self.assertEqual(2,len(mon['qualified_release_history']))
                self.assertEqual(4,len(mon['admission_hooks']))
                for defect in ('run','namespace','config','release'):
                    saved=copy.deepcopy(proof);lab.write_private.reset_mock()
                    if defect=='run':proof['run_id']='foreign'
                    if defect=='namespace':proof['monitor_namespace_uid']='foreign'
                    if defect=='config':proof['release_history'][1]['config_sha256']='different'
                    if defect=='release':proof['release_history'][1]['secret_release_sha256']='foreign'
                    save()
                    with self.subTest(defect=defect),self.assertRaises(ValueError):GATE.qualify_reentry(lab,123)
                    lab.write_private.assert_not_called();proof.clear();proof.update(saved)

    def test_cleanup_persists_csi_intent_before_pvc_delete_and_resumes_after_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);path=base/GATE.RECORD;path.write_text('{}');(base/'config.yml').write_text('original')
            row=row_fixture();row.update(original_input='original',allowed_inputs=['original'],created_crds={},storage_class_uid='sc')
            saved=next(iter(row['bindings'].values()));claim_name=next(iter(row['bindings']))
            claim={'kind':'PersistentVolumeClaim','metadata':{'name':claim_name,'uid':saved['claim_uid']},
                'spec':{'storageClassName':GATE.CLASS,'volumeName':saved['pv']}}
            pv={'metadata':{'uid':saved['pv_uid']},'spec':{'claimRef':{'uid':saved['claim_uid']},'storageClassName':GATE.CLASS,
                'persistentVolumeReclaimPolicy':'Delete','csi':{'driver':'driver.longhorn.io','volumeHandle':saved['volume']}}}
            volume={'metadata':{'uid':saved['volume_uid']}};events=[];state={'claim':claim,'pv':pv,'volume':volume,'interrupt':True}
            def get(kind,name=None,*args,**kw):
                if kind=='pvc':return {'items':[]} if name is None else state['claim']
                if kind=='pv':return state['pv']
                if kind=='volumes.longhorn.io':return state['volume']
                if kind in ('pods','engines.longhorn.io','replicas.longhorn.io'):return {'items':[]}
                if kind in ('storageclass','validatingwebhookconfiguration'):return None
                raise AssertionError(kind)
            def remove(obj):
                self.assertEqual(claim,obj)
                self.assertEqual(saved['volume_uid'],row['cleanup_bindings'][saved['pv']]['volume_uid'])
                events.append('pvc-delete');state.update(claim=None,pv=None,volume=None)
                if state['interrupt']:state['interrupt']=False;raise KeyboardInterrupt()
            lab=SimpleNamespace(DIRECTORY=base,BASE=ROOT,get=Mock(side_effect=get),remove=remove,write_private=lambda p,v:events.append('persist'),
                wait=lambda label,timeout,fn:fn(),installation_health=lambda:events.append('health'))
            monitor=SimpleNamespace(verify=lambda *args:events.append('monitor'))
            with patch.object(GATE,'receipt',return_value=row),patch.object(GATE,'namespace_identity',side_effect=lambda l,r,k:{'metadata':{'uid':'ns'}} if k=='database_namespace' and state['claim'] else None), \
                    patch.object(GATE,'graph',side_effect=lambda *args:[claim] if state['claim'] else []),patch.object(GATE,'monitor',return_value=monitor), \
                    patch.object(GATE,'monitor_identity',return_value={}),patch.object(GATE,'apply',side_effect=lambda *args:events.append('input')):
                with self.assertRaises(KeyboardInterrupt):GATE.cleanup(lab,123)
                self.assertIn('cleanup_bindings',row);self.assertTrue(path.exists())
                self.assertFalse(any(call.args[0] == 'validatingwebhookconfiguration' for call in lab.get.call_args_list))
                self.assertLess(events.index('persist'),events.index('pvc-delete'))
                GATE.cleanup(lab,123)
            self.assertFalse(path.exists());self.assertEqual(['input','monitor','health'],events[-3:])

    def test_cleanup_real_graph_handoff_uninstalls_controller_before_deleting_journaled_webhook_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);row=row_fixture();row.update(original_input='original',allowed_inputs=['original'],created_crds={})
            (base/GATE.RECORD).write_text('{}');(base/'config.yml').write_text('original')
            ns={'kind':'Namespace','metadata':{'uid':row['operator_namespace_uid'],'name':row['operator_namespace'],
                'labels':{'kubeauto.io/longhorn-middleware-run':row['run_id']}}}
            deployment={'kind':'Deployment','metadata':{'uid':'deployment','name':'pxc-operator','annotations':{
                'meta.helm.sh/release-name':'pxc-operator','meta.helm.sh/release-namespace':row['operator_namespace']}}}
            secret={'kind':'Secret','metadata':{'uid':'webhook','name':'pxc-webhook-ssl'},
                'type':'Opaque','data':dict.fromkeys(('ca.crt','tls.crt','tls.key'),'unit-canary')}
            state={'namespace':ns,'objects':[secret,deployment]};trace=[]
            def get(kind,name=None,*args,**kwargs):
                if kind=='namespace':return state['namespace'] if name==row['operator_namespace'] else None
                if kind=='objects':return {'items':state['objects']}
                if kind in ('storageclass','validatingwebhookconfiguration'):return None
                raise AssertionError((kind,name))
            def execute(argv,**kwargs):
                if 'uninstall' in argv:trace.append('helm-uninstall');state['objects']=[secret]
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            def remove(obj):
                self.assertIs(obj,ns);self.assertEqual('webhook',row['operator_secret_uids']['pxc-webhook-ssl'])
                trace.append('namespace-delete');state['namespace']=None
            lab=SimpleNamespace(DIRECTORY=base,BASE=ROOT,get=get,execute=execute,remove=remove,
                write_private=lambda *args:None,kubectl=lambda *args,**kwargs:SimpleNamespace(stdout='objects'),
                wait=lambda label,timeout,fn:self.assertTrue(fn()),installation_health=lambda:trace.append('health'))
            monitor=SimpleNamespace(verify=lambda *args:trace.append('monitor'))
            with patch.object(GATE,'receipt',return_value=row),patch.object(GATE,'monitor',return_value=monitor), \
                    patch.object(GATE,'monitor_identity',return_value={}):
                GATE.cleanup(lab,123)
            self.assertEqual(['helm-uninstall','namespace-delete','monitor','health'],trace)
            self.assertFalse((base/GATE.RECORD).exists())

    def test_cleanup_reentry_after_namespace_and_crd_deletion_keeps_native_and_foreign_guards(self):
        for scenario in ('gone','owned-crd','foreign-crd','foreign-consumer','active-database'):
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);path=base/GATE.RECORD;path.write_text('{}');(base/'config.yml').write_text('original')
                row=row_fixture();row.update(original_input='original',allowed_inputs=['original'],
                    created_crds={GATE.PXC_CRDS[0]:'original-crd'})
                namespace={'kind':'Namespace','metadata':{'name':row['database_namespace'],'uid':row['database_namespace_uid'],
                    'labels':{'kubeauto.io/longhorn-middleware-run':row['run_id']}}}
                database={'kind':'PerconaXtraDBCluster','metadata':{'name':GATE.CLUSTER,'uid':row['pxc_uid']},
                    'spec':{'pxc':{'size':3,'volumeSpec':{'persistentVolumeClaim':{'storageClassName':GATE.CLASS}}},'haproxy':{'size':3}}}
                crd={'kind':'CustomResourceDefinition','metadata':{'name':GATE.PXC_CRDS[0],
                    'uid':'foreign' if scenario=='foreign-crd' else 'original-crd'}}
                state={'namespace':namespace if scenario=='active-database' else None,
                    'database':database if scenario=='active-database' else None,'crd':None if scenario=='gone' else crd};trace=[]
                def get(kind,name=None,*args,**kwargs):
                    trace.append(('get',kind,name))
                    if kind=='namespace':return state['namespace'] if name==row['database_namespace'] else None
                    if kind=='crd':return state['crd']
                    if kind==GATE.PXC_CRDS[0]:
                        if not state['crd']:raise ValueError('server does not have this resource type')
                        return state['database'] if name else {'items':[{'foreign':True}] if scenario=='foreign-consumer' else []}
                    if kind in ('validatingwebhookconfiguration','storageclass'):return None
                    if kind=='pods':return {'items':[]}
                    raise AssertionError((kind,name))
                def remove(obj):
                    trace.append(('remove',obj['kind']))
                    state[{'PerconaXtraDBCluster':'database','Namespace':'namespace','CustomResourceDefinition':'crd'}[obj['kind']]]=None
                lab=SimpleNamespace(DIRECTORY=base,BASE=ROOT,get=get,remove=remove,
                    wait=lambda label,timeout,fn:self.assertTrue(fn()),installation_health=lambda:trace.append('health'))
                with patch.object(GATE,'receipt',return_value=row),patch.object(GATE,'graph',return_value=[]), \
                        patch.object(GATE,'apply',side_effect=lambda *args:trace.append('restore-input')), \
                        patch.object(GATE,'monitor',return_value=SimpleNamespace(verify=lambda *args:trace.append('monitor'))), \
                        patch.object(GATE,'monitor_identity',return_value={}):
                    if scenario.startswith('foreign'):
                        with self.assertRaisesRegex(ValueError,'CRD replaced or still has native consumers'):GATE.cleanup(lab,1)
                        self.assertTrue(path.exists());self.assertFalse(any(t[0]=='remove' for t in trace if isinstance(t,tuple)))
                    else:
                        GATE.cleanup(lab,1);self.assertFalse(path.exists());self.assertEqual(['restore-input','monitor','health'],trace[-3:])
                if scenario=='gone':self.assertFalse(any(t[0]=='get' and t[1]==GATE.PXC_CRDS[0] for t in trace if isinstance(t,tuple)))
                if scenario=='active-database':
                    self.assertEqual(['PerconaXtraDBCluster','Namespace','CustomResourceDefinition'],[t[1] for t in trace if isinstance(t,tuple) and t[0]=='remove'])

    def test_real_cleanup_reenters_after_backup_unlink_and_late_health_failure_without_accepting_replacements(self):
        for defect in ('none','tampered','symlink','unknown-intent','permission'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);row=row_fixture();owner={k:row[k] for k in ('run_id','cluster_uid','namespace_uid')}
                backup=base/'backup.sql';backup.write_bytes(b'owned-backup');backup.chmod(0o600);info=backup.stat()
                row.update(original_input='original',allowed_inputs=['original'],backup_create_intent=backup.name,
                    logical_backup={'name':backup.name,'device':info.st_dev,'inode':info.st_ino,'bytes':info.st_size,
                        'sha256':hashlib.sha256(backup.read_bytes()).hexdigest()},
                    created_crds={name:'owned-'+name for name in GATE.PXC_CRDS},
                    cleanup_bindings={'pv':{'pv_uid':'original-pv','volume':'volume','volume_uid':'original-volume'}})
                path=base/GATE.RECORD;path.write_text(json.dumps(row));(base/'config.yml').write_text('original')
                def get(kind,*args,**kwargs):
                    if kind in ('namespace','crd','pv','volumes.longhorn.io','storageclass','validatingwebhookconfiguration'):return None
                    if kind in ('engines.longhorn.io','replicas.longhorn.io'):return {'items':[]}
                    raise AssertionError(kind)
                lab=SimpleNamespace(DIRECTORY=base,BASE=ROOT,owner=lambda:owner,get=get,remove=Mock(),
                    write_private=lambda p,r:p.write_text(json.dumps(r)),wait=lambda label,timeout,fn:self.assertTrue(fn()),
                    installation_health=Mock(side_effect=ValueError('late health failure')))
                monitor=SimpleNamespace(verify=Mock(),receipt=lambda lab:row['monitoring_identity'])
                with patch.object(GATE,'monitor',return_value=monitor):
                    with self.assertRaisesRegex(ValueError,'late health failure'):GATE.cleanup(lab,1)
                    self.assertTrue(path.exists());self.assertFalse(backup.exists())
                    # Business verification still requires the original private dump.
                    with self.assertRaises(FileNotFoundError):GATE.backup_file(lab,row)
                    lab.installation_health=Mock()
                    if defect=='tampered':backup.write_bytes(b'foreign-backup');backup.chmod(0o600)
                    if defect=='symlink':backup.symlink_to(base/'missing-target')
                    if defect=='unknown-intent':
                        row.pop('logical_backup');path.write_text(json.dumps(row));backup.write_bytes(b'unknown-create-outcome')
                    if defect=='permission':
                        with patch.object(GATE,'backup_file',side_effect=PermissionError('denied')):
                            with self.assertRaises(PermissionError):GATE.cleanup(lab,1)
                    elif defect=='none':GATE.cleanup(lab,1);self.assertFalse(path.exists())
                    else:
                        with self.assertRaises(ValueError):GATE.cleanup(lab,1)
                    if defect!='none':
                        self.assertTrue(path.exists());lab.installation_health.assert_not_called()
                    lab.remove.assert_not_called()

    def run_shell(self,mode='middleware',fail='',terminate=False):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for d in ('.venv/bin','kube-bin','tests/helpers','control'): (base/d).mkdir(parents=True)
            context=base/'control/config';context.write_text('original-context')
            script=(ROOT/'tests/helpers/longhorn-v1-middleware.sh').read_text().replace('/usr/local/kubeauto',str(base))
            script=script.replace('/var/tmp/kubeauto-longhorn-v1-middleware.',str(base/'run.')).replace('/root/.kube/config',str(context))
            (base/'runner.sh').write_text(script);shutil.copyfile(ROOT/'tests/helpers/run-durable-gate.sh',base/'tests/helpers/run-durable-gate.sh')
            fake=base/'.venv/bin/python';fake.write_text('''#!/usr/bin/env bash
set -eu
name="${1##*/}"; action="${2:-}"
echo "$name $action" >> "$MID_TRACE"
if [[ "$action" == relay-maintain ]]; then
 exec "$MID_PY" -c 'import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,lambda *a:exit(0)); Path(os.environ["MID_RELAY_READY"]).touch(); time.sleep(60)'
fi
if [[ "$action" == cleanup || "$action" == monitor-cleanup ]]; then
 "$MID_PY" -c 'import os,time; from pathlib import Path; p=Path(os.environ["MID_RELAY_READY"]); start=time.monotonic()
while not p.exists():
 assert time.monotonic()-start<3
 time.sleep(.01)'
fi
if [[ "$name:$action" == "$MID_FAIL" ]]; then exit 17; fi
if [[ "$action" == setup ]]; then echo changed-context > "$MID_CONTEXT"; fi
if [[ "$name" == longhorn-v1-middleware.py && "$action" == business && "$MID_TERMINATE" == yes ]]; then
 touch "$MID_READY"
 exec "$MID_PY" -c 'import time;time.sleep(2)'
fi
if [[ "$name:$action" == longhorn-v1-observability.py:setup ]]; then echo LONGHORN_V1_MONITORING_SETUP_PASS; fi
if [[ "$name:$action" == longhorn-v1-middleware.py:setup ]]; then echo LONGHORN_MIDDLEWARE_SETUP_PASS; fi
if [[ "$name:$action" == longhorn-v1-middleware.py:business ]]; then echo LONGHORN_MIDDLEWARE_BUSINESS_PASS; fi
if [[ "$name:$action" == longhorn-v1-middleware.py:cleanup ]]; then echo LONGHORN_MIDDLEWARE_CONSUMERS_SCOPED_CLEAN_PASS; fi
if [[ "$name:$action" == longhorn-v1-middleware.py:qualify-reentry ]]; then echo LONGHORN_MIDDLEWARE_REENTRY_QUALIFICATION_PASS; fi
''');fake.chmod(0o755)
            kubectl=base/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',MID_TRACE=str(base/'trace'),MID_FAIL=fail,MID_PY=sys.executable,
                MID_RELAY_READY=str(base/'relay'),MID_READY=str(base/'ready'),MID_CONTEXT=str(context),MID_TERMINATE='yes' if terminate else 'no')
            process=subprocess.Popen(['bash',str(base/'runner.sh'),mode],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            if terminate:
                deadline=time.monotonic()+5
                while not (base/'ready').exists() and process.poll() is None:
                    if time.monotonic()>=deadline:process.kill();self.fail('business action did not start')
                    time.sleep(.01)
                process.send_signal(signal.SIGTERM)
            stdout,stderr=process.communicate(timeout=10)
            work=list(base.glob('run.*'));durable={p.name:p.read_text().strip() for p in work[0].iterdir() if p.name in ('exit','finalized')} if work else {}
            return process.returncode,stdout,stderr,(base/'trace').read_text() if (base/'trace').exists() else '',durable,context.read_text()

    def test_shell_full_owns_stages_cleanup_dependency_order_durable_zero_and_global_context_restore(self):
        rc,stdout,stderr,trace,durable,context=self.run_shell()
        self.assertEqual(0,rc,stdout+stderr);self.assertEqual({'exit':'0','finalized':'0'},durable)
        self.assertLess(trace.index('longhorn-v1-middleware.py cleanup'),trace.index('longhorn-v1-observability.py monitor-cleanup'))
        self.assertEqual(1,stdout.splitlines().count('LONGHORN_MIDDLEWARE_PASS'));self.assertEqual('original-context',context)
        self.assertIn('longhorn-v1-middleware.py verify',trace)

    def test_shell_stage_failure_stops_dependents_preserves_first_exit_and_context(self):
        for fail in ('longhorn-v1-observability.py:setup','longhorn-v1-middleware.py:setup',
                     'longhorn-v1-middleware.py:business','longhorn-v1-middleware.py:verify'):
            rc,stdout,stderr,trace,durable,context=self.run_shell(fail=fail)
            with self.subTest(fail=fail):
                self.assertEqual(17,rc,stdout+stderr);self.assertEqual('17',durable['exit']);self.assertIn('LONGHORN_FIRST_FAILURE',stderr)
                self.assertNotIn('LONGHORN_MIDDLEWARE_PASS',stdout);self.assertEqual('original-context',context)
                if fail.endswith(':setup'):self.assertNotIn('longhorn-v1-middleware.py business',trace)

    def test_shell_database_cleanup_failure_retains_monitoring_and_no_success(self):
        rc,stdout,stderr,trace,durable,context=self.run_shell(fail='longhorn-v1-middleware.py:cleanup')
        self.assertEqual(17,rc);self.assertEqual('17',durable['finalized']);self.assertNotIn('longhorn-v1-observability.py monitor-cleanup',trace)
        self.assertNotIn('LONGHORN_MIDDLEWARE_PASS',stdout);self.assertIn('LONGHORN_CLEANUP_FAIL',stderr);self.assertEqual('original-context',context)

    def test_shell_cleanup_only_reaches_recovery_before_empty_baseline_and_has_separate_terminal(self):
        rc,stdout,stderr,trace,durable,context=self.run_shell('middleware-cleanup')
        self.assertEqual(0,rc,stdout+stderr);self.assertIn('LONGHORN_MIDDLEWARE_CLEANUP_PASS',stdout)
        self.assertNotIn('longhorn-v1-middleware.py setup',trace);self.assertNotIn('longhorn-v1-middleware.py business',trace)
        self.assertLess(trace.index('longhorn-v1-middleware.py cleanup'),trace.index('longhorn-v1-observability.py baseline'))
        self.assertNotIn('LONGHORN_MIDDLEWARE_PASS',stdout)

    def test_reentry_cleanup_requires_qualification_before_any_consumer_deletion(self):
        rc,stdout,stderr,trace,durable,context=self.run_shell('middleware-reentry-cleanup')
        self.assertEqual(0,rc,stdout+stderr);self.assertEqual({'exit':'0','finalized':'0'},durable)
        self.assertLess(trace.index('longhorn-v1-middleware.py qualify-reentry'),trace.index('longhorn-v1-middleware.py cleanup'))
        rc,stdout,stderr,trace,durable,context=self.run_shell('middleware-reentry-cleanup',fail='longhorn-v1-middleware.py:qualify-reentry')
        self.assertEqual(17,rc);self.assertNotIn('longhorn-v1-middleware.py cleanup',trace)
        self.assertNotIn('longhorn-v1-observability.py monitor-cleanup',trace)

    def test_shell_sigterm_has_durable_143_cleanup_and_context_restore(self):
        rc,stdout,stderr,trace,durable,context=self.run_shell(terminate=True)
        self.assertEqual(143,rc,stdout+stderr);self.assertEqual({'exit':'143','finalized':'0'},durable)
        self.assertIn('longhorn-v1-middleware.py cleanup',trace);self.assertIn('longhorn-v1-observability.py monitor-cleanup',trace)
        self.assertEqual('original-context',context);self.assertNotIn('LONGHORN_MIDDLEWARE_PASS',stdout)

    def test_unknown_mode_cannot_reach_lab(self):
        rc,stdout,stderr,trace,durable,context=self.run_shell('unknown')
        self.assertEqual(2,rc);self.assertFalse(trace);self.assertFalse(durable)


if __name__=='__main__':unittest.main()
