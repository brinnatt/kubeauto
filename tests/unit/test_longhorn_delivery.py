"""Deterministic Longhorn input, pinned upstream API and destructive-boundary contracts."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml
from jsonschema import Draft7Validator

from common.constants import KubeConstant

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / 'roles/longhorn/files'


def module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), FILES / filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


product = module('longhorn.py')
host = module('host-check.py')


def configuration():
    c = yaml.safe_load((ROOT / 'roles/longhorn/defaults/main.yml').read_text())
    c.update(longhorn_install=True, longhorn_grpc_tls_required=False,
             longhorn_storage_nodes=['192.168.122.212', '192.168.122.165', '192.168.122.238'])
    c['longhorn_inventory_nodes'] = c['longhorn_storage_nodes'] + ['192.168.122.135']
    c['longhorn_client_nodes'] = list(c['longhorn_storage_nodes'])
    return c


class FakeAPI:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.reads = []
        self.prefix = ['kubectl']
        self.delete_propagation = []

    def get(self, kind, object_name='', namespace=''):
        key = (kind, object_name, namespace)
        self.reads.append(key)
        if key in self.objects:
            return copy.deepcopy(self.objects[key])
        return None if object_name else {'items': []}

    def put(self, kind, obj, namespace=''):
        self.objects[kind, obj['metadata']['name'], namespace] = obj

    def listing(self, kind, items, namespace=''):
        self.objects[kind, '', namespace] = {'items': items}

    def apply(self, obj, **kwargs):
        self.writes.append(('apply', obj))
        return obj

    def patch(self, *args):
        self.writes.append(('patch', args))

    def delete(self, obj, *, propagation_policy=None):
        self.writes.append(('delete', obj))
        self.delete_propagation.append(propagation_policy)


def native(kind, object_name, spec=None):
    obj = product.resource(kind, object_name, spec)
    obj['metadata'].update(uid='uid-' + object_name, resourceVersion='1')
    return obj


def recurring_values(directory, jobs=None):
    values = Path(directory)/'values.json'
    identity = {'cluster_uid': 'cluster', 'namespace_uid': 'namespace'}
    values.with_name('installation.json').write_text(json.dumps(identity))
    if jobs is not None:
        values.with_name('recurring-jobs.json').write_text(json.dumps(dict(identity, jobs=jobs)))
    return values


class InputContracts(unittest.TestCase):
    def test_foreground_delete_keeps_uid_and_resource_version_preconditions(self):
        obj = native('RecurringJob', 'owned', {})
        discovery = json.dumps({'resources': [{'kind': 'RecurringJob', 'name': 'recurringjobs'}]})
        for propagation in (None, 'Foreground'):
            with self.subTest(propagation=propagation), patch.object(product, 'run', return_value=discovery) as run:
                product.API('/private/config').delete(obj, propagation_policy=propagation)
                options = json.loads(run.call_args.kwargs['stdin'])
                self.assertEqual(options['preconditions'], {'uid': obj['metadata']['uid'], 'resourceVersion': '1'})
                if propagation:
                    self.assertEqual(options['propagationPolicy'], propagation)
                else:
                    self.assertNotIn('propagationPolicy', options)

    def test_api_get_places_namespace_flags_on_the_get_subcommand(self):
        for namespace, scope in (('*', ['-A']), ('longhorn-system', ['-n', 'longhorn-system']), ('', [])):
            with self.subTest(namespace=namespace), patch.object(product, 'run', return_value='{"items":[]}') as run:
                api = product.API('/private/cluster.kubeconfig')
                self.assertEqual(api.get('deployments', namespace=namespace), {'items': []})
                self.assertEqual(run.call_args.args[0], api.prefix+['get', 'deployments']+scope+['-o', 'json'])

    def test_defaults_stay_disabled_and_synchronized(self):
        c = yaml.safe_load((ROOT / 'conf/config.yml').read_text())
        defaults = yaml.safe_load((ROOT / 'roles/longhorn/defaults/main.yml').read_text())
        # Product config uses version constants; all other fields have one contract.
        for key, value in defaults.items():
            if key != 'longhorn_version':
                self.assertEqual(c[key], value, key)
        self.assertFalse(product.flag(c['longhorn_install']))
        self.assertEqual(KubeConstant().v_longhorn, defaults['longhorn_version'])

    def test_disabled_cli_never_uses_cluster_or_helm(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'config.json'
            p.write_text('{"longhorn_install":false}')
            with patch.object(sys, 'argv', ['longhorn.py', 'reconcile', '--config', str(p)]), \
                    patch.object(product, 'API', side_effect=AssertionError('cluster read')), \
                    patch.object(product, 'run', side_effect=AssertionError('external command')), redirect_stdout(StringIO()) as stream:
                product.main()
            self.assertEqual(stream.getvalue().strip(), 'LONGHORN_DISABLED_PASS')
            self.assertEqual(list(Path(directory).iterdir()), [p])

    def test_valid_baseline(self):
        product.validate(configuration())

    def test_documented_periodic_full_backup_configuration_reaches_native_spec(self):
        text = (ROOT/'docs/storage/longhorn/operations-manual.md').read_text()
        example = next(yaml.safe_load(block.split('```', 1)[0]) for block in text.split('```yaml\n')[1:]
                       if block.startswith('longhorn_recurring_jobs:'))
        c = configuration()
        c.update(example, longhorn_backup_target='s3://approved@us-east-1/', longhorn_backup_secret='backup')
        product.validate(c)
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as directory:
            values = recurring_values(directory)
            original_apply = api.apply
            def apply(obj, **kwargs):
                result = original_apply(obj)
                result['metadata']['uid'] = 'created-job'
                return result
            with patch.object(api, 'apply', side_effect=apply):
                product.reconcile_jobs(c, api, values)
            ledger = json.loads(values.with_name('recurring-jobs.json').read_text())
            self.assertEqual(ledger['jobs'], {'production-backup': 'created-job'})
        applied = [row[1] for row in api.writes if row[0] == 'apply']
        self.assertEqual(applied[0]['spec']['parameters'], {'full-backup-interval': '7'})

    def test_recurring_backup_parameters_and_native_name_limit_reject_before_mutation(self):
        baseline = {'name': 'backup', 'task': 'backup', 'cron': '0 */6 * * *', 'retain': 14,
                    'concurrency': 1, 'groups': ['production'], 'parameters': {'full-backup-interval': '7'}}
        for change in ({'name': 'a'*41}, {'parameters': {'unknown': '1'}},
                       {'parameters': {'full-backup-interval': 7}},
                       {'parameters': {'full-backup-interval': '-1'}},
                       {'parameters': {'full-backup-interval': 'NaN'}}, {'task': 'snapshot'}):
            c = configuration()
            c.update(longhorn_recurring_jobs=[dict(baseline, **change)],
                     longhorn_backup_target='s3://approved@us-east-1/', longhorn_backup_secret='backup')
            with self.subTest(change=change), self.assertRaises(ValueError):
                product.validate(c)

    def test_recurring_update_uses_put_with_original_identity_and_preserves_native_fields(self):
        api = product.API('/private/kubeconfig')
        obj = native('RecurringJob', 'schedule', {'cron': '0 0 1 1 *'})
        with patch.object(product, 'run', return_value=json.dumps(obj)) as run:
            api.replace(obj)
        self.assertIn('replace', run.call_args.args[0])
        self.assertNotIn('--force-conflicts', run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs['stdin'])['metadata'], obj['metadata'])
        c = configuration(); api = FakeAPI()
        job = {'name': 'schedule', 'task': 'backup', 'cron': '0 1 1 1 *', 'retain': 2, 'concurrency': 1, 'groups': []}
        current = native('RecurringJob', 'schedule', dict(job, cron='0 0 1 1 *', labels={'business': 'keep'},
                         parameters={'full-backup-interval': '7'}))
        current['metadata'].update(resourceVersion='original-rv', finalizers=['longhorn.io'])
        api.put('recurringjobs.longhorn.io', current, product.NAMESPACE)
        c['longhorn_recurring_jobs'] = [job]
        def replace(obj):
            self.assertEqual(obj['metadata'], current['metadata'])
            self.assertEqual(obj['spec']['labels'], {'business': 'keep'})
            self.assertEqual(obj['spec']['parameters'], {}, 'removing optional parameters must clear their old values')
            api.put('recurringjobs.longhorn.io', obj, product.NAMESPACE)
            return copy.deepcopy(obj)
        with tempfile.TemporaryDirectory() as directory, patch.object(api, 'replace', side_effect=replace, create=True) as put:
            values = recurring_values(directory, {'schedule': current['metadata']['uid']})
            product.reconcile_jobs(c, api, values)
            product.reconcile_jobs(c, api, values)
            self.assertEqual(put.call_count, 1, 'identical input must not rewrite the plan')
            self.assertEqual(api.writes, [])

    def test_removed_recurring_job_waits_for_native_cleanup_without_touching_foreign_jobs(self):
        c = configuration(); api = FakeAPI()
        own = native('RecurringJob', 'retired', {})
        foreign = copy.deepcopy(own); foreign['metadata'].update(name='foreign', labels={})
        partial = copy.deepcopy(own); partial['metadata'].update(name='partial', labels=dict(product.OWNER))
        api.listing('recurringjobs.longhorn.io', [own, foreign, partial], product.NAMESPACE)
        api.put('recurringjobs.longhorn.io', own, product.NAMESPACE)
        cronjob = {'metadata': {'name': 'retired-cj', 'ownerReferences': [{'uid': own['metadata']['uid']}]}}
        api.listing('cronjobs', [cronjob], product.NAMESPACE)
        def waited(label, timeout, ready):
            self.assertFalse(ready())
            api.objects['recurringjobs.longhorn.io', 'retired', product.NAMESPACE] = None
            self.assertFalse(ready())  # CR deletion alone does not prove the schedule stopped.
            api.listing('cronjobs', [], product.NAMESPACE)
            self.assertTrue(ready())
        with tempfile.TemporaryDirectory() as directory, patch.object(product, 'wait', side_effect=waited):
            values = recurring_values(directory, {'retired': own['metadata']['uid']})
            product.reconcile_jobs(c, api, values)
            self.assertEqual(json.loads(values.with_name('recurring-jobs.json').read_text())['jobs'], {})
        self.assertEqual(api.writes, [('delete', own)])
        self.assertEqual(api.delete_propagation, ['Foreground'])

    def test_recurring_job_cleanup_rejects_identity_replacement_and_foreign_name_collision(self):
        c = configuration(); api = FakeAPI(); own = native('RecurringJob', 'retired', {})
        api.listing('recurringjobs.longhorn.io', [own], product.NAMESPACE)
        replacement = copy.deepcopy(own); replacement['metadata']['uid'] = 'replacement'
        api.put('recurringjobs.longhorn.io', replacement, product.NAMESPACE)
        with tempfile.TemporaryDirectory() as directory:
            values = recurring_values(directory, {'retired': own['metadata']['uid']})
            with self.assertRaisesRegex(ValueError, 'replaced'):
                product.reconcile_jobs(c, api, values)
        api.writes.clear()
        c['longhorn_recurring_jobs'] = [{'name': 'foreign'}]
        replacement['metadata']['labels'] = {}
        api.put('recurringjobs.longhorn.io', replacement, product.NAMESPACE)
        api.objects['recurringjobs.longhorn.io', 'foreign', product.NAMESPACE] = replacement
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'foreign resource'):
            product.reconcile_jobs(c, api, recurring_values(directory))
        self.assertEqual(api.writes, [])

    def test_recurring_receipt_prevents_label_only_adoption_and_cross_installation_cleanup(self):
        c = configuration(); api = FakeAPI(); obj = native('RecurringJob', 'old', {})
        api.put('recurringjobs.longhorn.io', obj, product.NAMESPACE)
        api.put('crd', {'metadata': {'name': 'recurringjobs.longhorn.io'}})
        with tempfile.TemporaryDirectory() as directory:
            values = recurring_values(directory)
            product.reconcile_jobs(c, api, values)
            self.assertEqual(api.writes, [], 'labels without a receipt cannot authorize deletion')
            c['longhorn_recurring_jobs'] = [{'name': 'old'}]
            with self.assertRaisesRegex(ValueError, 'UID receipt'):
                product.check_recurring_identity(c, api, values)
            with self.assertRaisesRegex(ValueError, 'UID receipt'):
                product.reconcile_jobs(c, api, values)
            path = values.with_name('recurring-jobs.json')
            path.write_text(json.dumps({'cluster_uid': 'foreign', 'namespace_uid': 'namespace', 'jobs': {'old': obj['metadata']['uid']}}))
            with self.assertRaisesRegex(ValueError, 'another installation'):
                product.reconcile_jobs(c, api, values)
            self.assertEqual(api.writes, [])

    def test_recurring_receipt_archives_only_after_uninstall_guard(self):
        c = configuration(); c.update(longhorn_action='uninstall', longhorn_uninstall_id='retire')
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as directory:
            values = recurring_values(directory, {'old': 'old-uid'})
            path = values.with_name('recurring-jobs.json'); original = path.read_bytes()
            with patch.object(product, 'guard', side_effect=ValueError('data remains')):
                with self.assertRaisesRegex(ValueError, 'data remains'):
                    product.reconcile(c, api, 'helm', values, {}, None)
            self.assertEqual(path.read_bytes(), original)
            with patch.object(product, 'guard'), patch.object(product, 'release_node_bootstrap', return_value=False), redirect_stdout(StringIO()):
                product.reconcile(c, api, 'helm', values, {}, None)
            self.assertFalse(path.exists())
            self.assertEqual(values.with_name('recurring-jobs-uninstalled-namespace.json').read_bytes(), original)

    def test_s3_target_requires_path_consumed_by_pinned_backupstore(self):
        # backupstore e5f4ddc47e4d s3/s3.go rejects an empty url.Path.
        # A nonempty prefix without a trailing slash remains valid upstream.
        for suffix in ('', '/', '/production', '/production/'):
            config = configuration()
            config.update(longhorn_backup_target='s3://backups@us-east-1'+suffix,
                          longhorn_backup_secret='backup-credentials')
            with self.subTest(suffix=suffix), patch.object(product, 'run', side_effect=AssertionError('command')):
                if suffix: product.validate(config)
                else:
                    with self.assertRaisesRegex(ValueError, 'backup target'): product.validate(config)

    def test_invalid_inputs_reject_without_commands(self):
        invalid = {'longhorn_version': '1.13.0', 'longhorn_namespace': 'default',
                   'longhorn_action': 'delete', 'longhorn_image_prefix': 'https://user:password@registry/repo',
                   'longhorn_storage_nodes': ['192.168.122.212'], 'longhorn_data_path': '/',
                   'longhorn_ready_timeout': True, 'longhorn_snapshot_controller': 'auto',
                   'longhorn_backup_target': 's3://user:password@bucket', 'longhorn_storage_classes': [],
                   'longhorn_install': 'maybe', 'longhorn_requests': [{'kind': 'erase', 'name': 'x'}]}
        for key, value in invalid.items():
            with self.subTest(key=key), patch.object(product, 'run', side_effect=AssertionError('command')):
                c = configuration(); c[key] = value
                with self.assertRaises(ValueError):
                    product.validate(c)

    def test_storage_nodes_must_be_unique_and_in_inventory(self):
        for nodes in [['192.168.122.212'] * 3, ['192.168.122.212', '192.168.122.165', 'unknown']]:
            c = configuration(); c['longhorn_storage_nodes'] = nodes
            with self.assertRaises(ValueError):
                product.validate(c)

    def test_storage_nodes_require_the_real_workload_inventory(self):
        for clients in ([], ['unknown'], ['192.168.122.212'] * 3, 'all', None):
            c = configuration(); c['longhorn_client_nodes'] = clients
            with self.subTest(clients=clients), self.assertRaises(ValueError): product.validate(c)

    def test_health_requires_a_running_v1_instance_manager_on_every_client(self):
        c = configuration(); c['longhorn_snapshot_controller'] = 'disabled'
        c['longhorn_storage_nodes'] = []
        nodes = {address: native('Node', 'client-' + str(i)) for i, address in enumerate(c['longhorn_inventory_nodes'])}
        clients = [nodes[address]['metadata']['name'] for address in c['longhorn_client_nodes']]
        api = FakeAPI()
        for name, count in (('longhorn-manager', 3), ('longhorn-csi-plugin', 4)):
            obj = native('DaemonSet', name)
            obj['status'] = {'observedGeneration': 1, 'numberReady': count, 'updatedNumberScheduled': count}
            api.put('daemonset', obj, product.NAMESPACE)
        for name in ('csi-attacher', 'csi-provisioner', 'csi-resizer', 'csi-snapshotter', 'longhorn-ui'):
            obj = native('Deployment', name, {'replicas': 1})
            obj['status'] = {'availableReplicas': 1, 'updatedReplicas': 1}
            api.put('deployment', obj, product.NAMESPACE)
        lh_nodes = [dict(native('Node', name, {'disks': {}}), status={'conditions': [{'type': 'Ready', 'status': 'True'}]}) for name in clients]
        # Dedicated cordoned control planes have no Manager/IM and are not volume clients.
        lh_nodes.append(native('Node', 'client-3', {'disks': {}}))
        api.listing('nodes.longhorn.io', lh_nodes, product.NAMESPACE)
        image = native('EngineImage', 'engine', {'image': c['longhorn_image_prefix']+'/longhorn-engine:v1.12.1'})
        image['status'] = {'state': 'deployed', 'nodeDeploymentMap': {name: True for name in clients}}
        api.listing('engineimages.longhorn.io', [image], product.NAMESPACE)
        api.put('csidriver', native('CSIDriver', 'driver.longhorn.io'))
        for fault in ('none', 'missing', 'stopped', 'not-ready', 'wrong-engine', 'wrong-image', 'deleting'):
            ims = [dict(native('InstanceManager', name, {'nodeID': name, 'dataEngine': 'v1',
                       'image': c['longhorn_image_prefix']+'/longhorn-instance-manager:v1.12.1-kubeauto.3'}),
                       status={'currentState': 'running', 'conditions': [{'type': 'PodReady', 'status': 'True'}]}) for name in clients]
            if fault == 'missing': ims.pop()
            if fault == 'stopped': ims[0]['status']['currentState'] = 'stopped'
            if fault == 'not-ready': ims[0]['status']['conditions'] = []
            if fault == 'wrong-engine': ims[0]['spec']['dataEngine'] = 'v2'
            if fault == 'wrong-image': ims[0]['spec']['image'] = c['longhorn_image_prefix']+'/longhorn-instance-manager:v1.12.1'
            if fault == 'deleting': ims[0]['metadata']['deletionTimestamp'] = 'now'
            api.listing('instancemanagers.longhorn.io', ims, product.NAMESPACE)
            with self.subTest(fault=fault): self.assertEqual(product.healthy(c, api, nodes), fault == 'none')

    def test_uninstall_requires_explicit_id(self):
        c = configuration(); c['longhorn_action'] = 'uninstall'
        with self.assertRaisesRegex(ValueError, 'change ID'):
            product.validate(c)

    def test_initial_null_disk_status_is_not_ready_for_either_engine(self):
        for engine in ('v1', 'v2'):
            c = configuration()
            address = c['longhorn_client_nodes'][0]
            c['longhorn_client_nodes'] = [address]
            c['longhorn_storage_nodes'] = [address] if engine == 'v1' else []
            c['longhorn_data_engines'] = [engine]
            c['longhorn_block_disks'] = {} if engine == 'v1' else {address: [{
                'name': 'data', 'path': '/dev/disk/by-id/wwn-unit',
                'disk_driver': 'aio', 'storage_reserved': 0}]}
            nodes = {address: native('Node', 'new-node')}
            disk = {'path': c['longhorn_data_path']} if engine == 'v1' else product.block_declarations(c, address)['data']
            api = FakeAPI()
            for name in ('longhorn-manager', 'longhorn-csi-plugin'):
                obj = native('DaemonSet', name)
                obj['status'] = {'observedGeneration': 1, 'numberReady': 1, 'updatedNumberScheduled': 1}
                api.put('daemonset', obj, product.NAMESPACE)
            for name in ('csi-attacher', 'csi-provisioner', 'csi-resizer', 'csi-snapshotter', 'longhorn-ui'):
                obj = native('Deployment', name, {'replicas': 1})
                obj['status'] = {'availableReplicas': 1, 'updatedReplicas': 1}
                api.put('deployment', obj, product.NAMESPACE)
            for pending in (None, {}, {'data': None}, {'data': {}}):
                obj = native('Node', 'new-node', {'disks': {'data': disk}})
                obj['status'] = {'conditions': [{'type': 'Ready', 'status': 'True'}], 'diskStatus': pending}
                api.listing('nodes.longhorn.io', [obj], product.NAMESPACE)
                with self.subTest(engine=engine, status=pending):
                    self.assertFalse(product.healthy(c, api, nodes))
                    self.assertEqual([], api.writes)

    def test_class_safety_defaults(self):
        sc = product.storage_class(configuration()['longhorn_storage_classes'][0])
        self.assertEqual(sc['reclaimPolicy'], 'Retain')
        self.assertEqual(sc['parameters']['dataEngine'], 'v1')
        self.assertEqual(sc['parameters']['replicaSoftAntiAffinity'], 'disabled')
        self.assertTrue(sc['allowVolumeExpansion'])
        self.assertEqual(sc['metadata']['annotations']['storageclass.kubernetes.io/is-default-class'], 'false')

    def test_encrypted_class_secret_references(self):
        c = configuration(); sc = c['longhorn_storage_classes'][0]
        sc.update(encrypted=True, encryption_secret='key')
        product.validate(c)
        params = product.storage_class(sc)['parameters']
        self.assertEqual(params['csi.storage.k8s.io/node-stage-secret-name'], 'key')
        self.assertEqual(params['csi.storage.k8s.io/node-expand-secret-namespace'], product.NAMESPACE)
        self.assertEqual(params['encrypted'], 'true')


class OwnershipContracts(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI(); self.c = configuration()

    def test_node_bootstrap_refreshes_version_and_only_retries_api_conflicts(self):
        expected = {'metadata': {'name': 'worker', 'uid': 'original', 'resourceVersion': 'stale'}}
        for fault in ('none', 'replaced', 'foreign', 'foreign-client', 'permission', 'exhausted', 'retry-replaced'):
            with self.subTest(fault=fault):
                nodes = [copy.deepcopy(expected) for _ in range(3)]
                for n, node in enumerate(nodes): node['metadata']['resourceVersion'] = str(100+n)
                if fault == 'replaced': nodes[0]['metadata']['uid'] = 'replacement'
                if fault == 'retry-replaced': nodes[1]['metadata']['uid'] = 'replacement'
                if fault == 'foreign': nodes[0]['metadata']['labels'] = {'node.longhorn.io/create-default-disk': 'true'}
                if fault == 'foreign-client': nodes[0]['metadata']['labels'] = {'kubeauto.io/longhorn-client': 'true'}
                conflict = product.CommandError('kubectl', 1, 'Error from server (Conflict): object has been modified')
                denied = product.CommandError('kubectl', 1, 'Error from server (Forbidden): denied')
                errors = [denied] if fault == 'permission' else [conflict]*3 if fault == 'exhausted' else [conflict, {}]
                with patch.object(self.api, 'get', side_effect=nodes), patch.object(self.api, 'patch', side_effect=errors) as apply, \
                        patch.object(product.time, 'sleep'):
                    if fault == 'none':
                        product.bootstrap_node(self.c, self.api, self.c['longhorn_storage_nodes'][0], expected, {'total_bytes': 1000})
                        self.assertEqual(apply.call_count, 2)
                        self.assertEqual([c.args[2]['metadata']['resourceVersion'] for c in apply.call_args_list], ['100', '101'])
                        self.assertEqual(apply.call_args.args[2]['metadata']['annotations'], {
                            'node.longhorn.io/default-disks-config': json.dumps([{'name': 'kubeauto-data',
                                'path': self.c['longhorn_data_path'], 'allowScheduling': True, 'storageReserved': 100}])})
                    else:
                        with self.assertRaises((ValueError, product.CommandError)):
                            product.bootstrap_node(self.c, self.api, self.c['longhorn_storage_nodes'][0], expected, {'total_bytes': 1000})
                        self.assertEqual(apply.call_count, 0 if fault in ('replaced', 'foreign', 'foreign-client') else 3 if fault == 'exhausted' else 1)

    def test_command_failure_retains_private_reason_without_logging_payload(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(product.CommandError) as caught:
            product.run([sys.executable, '-c',
                         'import sys; sys.stderr.write("private-credential"); sys.exit(7)'])
        self.assertEqual(caught.exception.stderr, 'private-credential')
        self.assertEqual(caught.exception.returncode, 7)
        self.assertNotIn('private-credential', str(caught.exception) + output.getvalue())

    def test_node_bootstrap_control_node_and_identical_metadata(self):
        node = {'metadata': {'name': 'control', 'uid': 'original', 'resourceVersion': '10'}}
        with patch.object(self.api, 'get', return_value=node), patch.object(self.api, 'patch') as apply:
            product.bootstrap_node(self.c, self.api, 'control-address', node, None)
            desired = apply.call_args.args[2]['metadata']
            self.assertNotIn('annotations', desired)
            self.assertEqual(desired['labels']['node.longhorn.io/create-default-disk'], 'false')
            self.assertEqual(desired['labels']['kubeauto.io/longhorn-client'], 'false')
            node['metadata'].update(desired)
            apply.reset_mock()
            product.bootstrap_node(self.c, self.api, 'control-address', node, None)
            apply.assert_not_called()

    def test_foreign_namespace_rejects_before_external_commands(self):
        self.api.put('namespace', {'metadata': {'name': product.NAMESPACE}})
        with patch.object(product, 'run', side_effect=AssertionError('command')):
            with self.assertRaisesRegex(ValueError, 'foreign'):
                product.preflight(self.c, self.api, 'helm', '/unused/values.json')
        self.assertEqual(self.api.writes, [])

    def test_foreign_and_immutable_class_refused(self):
        sc = product.storage_class(self.c['longhorn_storage_classes'][0])
        for foreign in (True, False):
            obj = copy.deepcopy(sc)
            if foreign: obj['metadata']['labels'] = {}
            else: obj['parameters']['numberOfReplicas'] = '2'
            self.api.listing('storageclasses', [obj])
            with self.assertRaises(ValueError): product.check_classes(self.c, self.api)
        self.assertEqual(self.api.writes, [])

    def test_other_default_class_not_replaced(self):
        self.c['longhorn_storage_classes'][0]['default'] = True
        self.api.listing('storageclasses', [{'metadata': {'name': 'existing', 'annotations': {
            'storageclass.kubernetes.io/is-default-class': 'true'}}}])
        with self.assertRaisesRegex(ValueError, 'another default'):
            product.check_classes(self.c, self.api)

    def test_request_same_id_different_input_refused(self):
        old = product.request_object({'kind': 'snapshot', 'name': 'snapshot-1', 'volume': 'volume-1'})
        new = product.request_object({'kind': 'snapshot', 'name': 'snapshot-1', 'volume': 'volume-2'})
        product.check_request(old, copy.deepcopy(old))
        with self.assertRaisesRegex(ValueError, 'immutable'):
            product.check_request(old, new)
        old['metadata']['labels'] = {}
        with self.assertRaisesRegex(ValueError, 'foreign'):
            product.check_request(old, new)

    def test_request_preflight_happens_before_reconcile(self):
        self.c['longhorn_requests'] = [{'kind': 'snapshot', 'name': 'request-1', 'volume': 'volume-1'}]
        self.api.listing('crds', [{'metadata': {'name': 'snapshots.longhorn.io'}}])
        foreign = native('Snapshot', 'request-1', {'volume': 'volume-1'})
        foreign['metadata']['labels'] = {}
        self.api.put('Snapshot.longhorn.io', foreign, product.NAMESPACE)
        with self.assertRaisesRegex(ValueError, 'foreign'):
            product.check_declarations(self.c, self.api)
        self.assertEqual(self.api.writes, [])

    def test_optional_missing_apis_are_not_queried(self):
        product.check_declarations(self.c, self.api)
        self.assertFalse(any('longhorn.io' in k or k == 'volumesnapshotclass' for k, _, _ in self.api.reads))
        self.c['longhorn_monitoring_enabled'] = True
        with self.assertRaisesRegex(ValueError, 'ServiceMonitor CRD'):
            product.check_declarations(self.c, self.api)

    def test_managed_snapshot_rejects_foreign_or_duplicate_controller(self):
        deployment = next(o for o in product.snapshot_objects(self.c) if o['kind'] == 'Deployment')
        for items in ([{'metadata': {'name': 'foreign'}, 'spec': deployment['spec']}], [deployment, deployment]):
            self.api.listing('deployments', items, '*')
            with self.assertRaises(ValueError): product.snapshot_preflight(self.c, self.api)
        self.assertEqual(self.api.writes, [])

    def test_managed_snapshot_rejects_foreign_crd(self):
        self.api.put('CustomResourceDefinition', {'metadata': {'name': 'volumesnapshots.snapshot.storage.k8s.io'}})
        with self.assertRaisesRegex(ValueError, 'foreign'):
            product.snapshot_preflight(self.c, self.api)

    def test_external_snapshot_requires_healthy_exact_schema(self):
        self.c['longhorn_snapshot_controller'] = 'external'
        objects = product.snapshot_objects(self.c)
        deployment = next(o for o in objects if o['kind'] == 'Deployment')
        deployment['status'] = {'availableReplicas': 2}
        self.api.listing('deployments', [deployment], '*')
        for o in objects:
            if o['kind'] == 'CustomResourceDefinition':
                o['status'] = {'conditions': [{'type': 'Established', 'status': 'True'}]}
                self.api.put(o['kind'], o)
        self.assertEqual(product.snapshot_preflight(self.c, self.api), [])
        crd = self.api.objects['CustomResourceDefinition', 'volumesnapshots.snapshot.storage.k8s.io', '']
        crd['spec']['versions'][0]['schema']['openAPIV3Schema']['properties'].pop('spec')
        with self.assertRaisesRegex(ValueError, 'schema'):
            product.snapshot_preflight(self.c, self.api)

    def test_disabled_snapshot_does_not_inspect_shared_resources(self):
        self.c['longhorn_snapshot_controller'] = 'disabled'
        self.assertEqual(product.snapshot_preflight(self.c, self.api), [])
        self.assertEqual(self.api.reads, [])

    def test_first_install_refuses_existing_native_csi_driver_before_host_checks(self):
        for annotations in ({}, {'longhorn.io/csi-version': 'v1.12.1'}):
            with self.subTest(annotations=annotations), tempfile.TemporaryDirectory() as directory:
                driver = {'metadata': {'name': 'driver.longhorn.io', 'uid': 'foreign-driver',
                                      'annotations': annotations}, 'spec': {'podInfoOnMount': True}}
                self.api.put('csidriver', driver)
                values = Path(directory) / 'values.json'
                with self.assertRaisesRegex(ValueError, 'existing Longhorn CSI driver has no local installation receipt'):
                    product.check_installation_identity(self.api, values, None)
                with patch.object(product, 'run', return_value='[]'), \
                        patch.object(product, 'node_map', side_effect=AssertionError('identity check must finish first')):
                    with self.assertRaisesRegex(ValueError, 'existing Longhorn CSI driver has no local installation receipt'):
                        product.preflight(self.c, self.api, 'helm', values)
                self.assertFalse(values.with_name('installation.json').exists())
                self.assertEqual(self.api.writes, [])
                # Native CSI objects do not carry product labels. A receipt
                # bound to the actual installation still permits normal reentry.
                for name in (product.NAMESPACE, 'kube-system'):
                    self.api.put('namespace', native('Namespace', name))
                product.record_installation(self.api, values)
                product.check_installation_identity(self.api, values, {'name': 'longhorn'})

    def test_installation_receipt_prevents_cross_cluster_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / 'values.json'
            for object_name in (product.NAMESPACE, 'kube-system'):
                self.api.put('namespace', native('Namespace', object_name))
            product.record_installation(self.api, values)
            product.check_installation_identity(self.api, values, {'name': 'longhorn'})
            self.api.objects['namespace', 'kube-system', '']['metadata']['uid'] = 'different-cluster'
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                product.check_installation_identity(self.api, values, {})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'no local installation receipt'):
                product.check_installation_identity(self.api, Path(directory) / 'values.json', {'name': 'longhorn'})

    def test_native_extra_filesystem_disks_survive_product_preflight_without_mutation(self):
        self.api.put('crd', {'metadata': {'name': 'nodes.longhorn.io'}})
        nodes = {address: {'metadata': {'name': 'worker-' + str(index)}}
                 for index, address in enumerate(self.c['longhorn_storage_nodes'])}
        disks = {'primary': {'path': self.c['longhorn_data_path'], 'diskType': 'filesystem'},
                 'native-extra': {'path': '/data/extra', 'diskType': 'filesystem', 'storageReserved': 1234}}
        original = copy.deepcopy(disks)
        self.api.listing('nodes.longhorn.io', [native('Node', 'worker-0', {'disks': disks})], product.NAMESPACE)
        product.check_disk_declarations(self.c, self.api, nodes)
        self.assertEqual(disks, original)
        self.assertEqual(self.api.writes, [])
        for mutation in ('outside-allowlist', 'missing-primary', 'duplicate-primary', 'block-disk'):
            obj = native('Node', 'worker-0', {'disks': copy.deepcopy(original)})
            if mutation == 'outside-allowlist': obj['metadata']['name'] = 'foreign-data-node'
            elif mutation == 'missing-primary': obj['spec']['disks'].pop('primary')
            elif mutation == 'duplicate-primary': obj['spec']['disks']['native-extra']['path'] = self.c['longhorn_data_path']
            else: obj['spec']['disks']['native-extra']['diskType'] = 'block'
            self.api.listing('nodes.longhorn.io', [obj], product.NAMESPACE)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'allowlist'):
                product.check_disk_declarations(self.c, self.api, nodes)
        self.assertEqual(self.api.writes, [])

    def test_upgrade_rejects_downgrade_missing_id_or_wrong_system_backup(self):
        for old, target, change, backup_version, volumes, expected in (
                ('1.12.1', '1.12.0', 'change', '1.12.1', [], 'downgrade'),
                ('1.11.3', '1.12.1', 'change', '1.11.3', [], 'unsupported'),
                ('1.12.0', '1.12.1', '', '1.12.0', [], 'change ID'),
                ('1.12.0', '1.12.1', 'change', '1.11.3', [], 'source version'),
                ('1.12.0', '1.12.1', 'change', '1.12.0', [{'spec': {'dataEngine': 'v2'}, 'status': {'robustness': 'healthy'}}], 'disable the volume data engine')):
            with self.subTest(old=old, target=target, change=change, backup_version=backup_version, volumes=volumes):
                c = configuration(); c.update(longhorn_version=target, longhorn_upgrade_id=change,
                    longhorn_upgrade_system_backup='before-upgrade', longhorn_snapshot_controller='disabled')
                self.api.put('systembackups.longhorn.io', {'metadata': {'name': 'before-upgrade'},
                    'status': {'state': 'Ready', 'version': backup_version}}, product.NAMESPACE)
                self.api.listing('volumes.longhorn.io', volumes, product.NAMESPACE)
                release = {'name': 'longhorn', 'namespace': product.NAMESPACE, 'chart': 'longhorn-' + old, 'app_version': old}
                with patch.object(product, 'run', return_value=json.dumps([release])), \
                        patch.object(product, 'check_installation_identity'), patch.object(product, 'node_map', return_value={}), \
                        patch.object(product, 'check_secrets'):
                    with self.assertRaisesRegex(ValueError, expected): product.preflight(c, self.api, 'helm', '/unused')
                self.assertEqual(self.api.writes, [])

    def test_v2_upgrade_retains_detached_legacy_clones_but_preserves_data_and_stop_guards(self):
        for defect, reason in (('none', None), ('backing-image', 'backing images require migration'),
                ('attached', 'requires detached'), ('migration', 'requires detached'), ('unhealthy', 'requires healthy'),
                ('engine-running', 'stopped engine and replica'), ('replica-running', 'stopped engine and replica')):
            with self.subTest(defect=defect):
                api = FakeAPI(); c = configuration()
                c.update(longhorn_version='1.12.1', longhorn_data_engines=['v2'], longhorn_upgrade_id='approved-change',
                    longhorn_upgrade_system_backup='before-upgrade', longhorn_snapshot_controller='disabled')
                volume = {'metadata': {'name': 'old-linked', 'uid': 'original'},
                    'spec': {'dataEngine': 'v2', 'cloneMode': 'linked-clone', 'numberOfReplicas': 1},
                    'status': {'state': 'detached', 'robustness': 'healthy'}}
                if defect == 'backing-image': volume['spec']['backingImage'] = 'dependent-image'
                if defect == 'attached': volume['status']['state'] = 'attached'
                if defect == 'migration': volume['spec']['migrationNodeID'] = 'node'
                if defect == 'unhealthy': volume['status']['robustness'] = 'faulted'
                api.listing('volumes.longhorn.io', [volume], product.NAMESPACE)
                api.put('systembackups.longhorn.io', {'metadata': {'name': 'before-upgrade'},
                    'status': {'state': 'Ready', 'version': '1.12.0'}}, product.NAMESPACE)
                for plural in ('engines', 'replicas'):
                    state = 'running' if defect == plural.removesuffix('s')+'-running' else 'stopped'
                    api.listing(plural+'.longhorn.io', [{'spec': {'volumeName': 'old-linked'},
                        'status': {'currentState': state}}], product.NAMESPACE)
                release = {'name': 'longhorn', 'namespace': product.NAMESPACE, 'chart': 'longhorn-1.12.0', 'app_version': '1.12.0'}
                def run(args, **kwargs): return json.dumps([release]) if 'list' in args else ''
                with patch.object(product, 'run', side_effect=run), patch.object(product, 'node_map', return_value={}), \
                        patch.object(product, 'check_installation_identity'), patch.object(product, 'check_secrets'), \
                        patch.object(product, 'check_classes'), patch.object(product, 'check_declarations'), \
                        patch.object(product, 'check_recurring_identity'), patch.object(product, 'check_engine_changes'), \
                        patch.object(product, 'check_disk_declarations'), patch.object(product, 'check_block_identity'):
                    if reason:
                        with self.assertRaisesRegex(ValueError, reason): product.preflight(c, api, 'helm', '/unused')
                    else:
                        product.preflight(c, api, 'helm', '/unused')
                self.assertEqual(api.writes, [])

    def test_uninstall_without_release_does_not_require_hosts_or_secrets(self):
        self.c.update(longhorn_action='uninstall', longhorn_uninstall_id='change-1')
        with tempfile.TemporaryDirectory() as directory, patch.object(product, 'run', return_value='[]'), \
                patch.object(product, 'node_map', side_effect=AssertionError('node_map')), \
                patch.object(product, 'check_secrets', side_effect=AssertionError('secrets')), redirect_stdout(StringIO()):
            nodes, release = product.preflight(self.c, self.api, 'helm', Path(directory) / 'values.json')
            product.reconcile(self.c, self.api, 'helm', Path(directory) / 'values.json', nodes, release)
        self.assertEqual(self.api.writes, [])

    def test_uninstall_residue_rejects_without_release(self):
        self.c.update(longhorn_action='uninstall', longhorn_uninstall_id='change-1')
        self.api.listing('crds', [{'metadata': {'name': 'volumes.longhorn.io'}}])
        with tempfile.TemporaryDirectory() as directory, patch.object(product, 'run', return_value='[]'):
            with self.assertRaisesRegex(ValueError, 'installation receipt'):
                product.preflight(self.c, self.api, 'helm', Path(directory) / 'values.json')
        self.assertEqual(self.api.writes, [])

    def test_partial_install_uninstall_removes_only_owned_node_bootstrap(self):
        node = native('Node', 'worker')
        node['metadata']['labels']['unrelated'] = 'keep'
        node['metadata']['labels']['node.longhorn.io/create-default-disk'] = 'config'
        node['metadata']['annotations'] = {'node.longhorn.io/default-disks-config': '[]', 'unrelated': 'keep'}
        self.api.listing('nodes', [node, {'metadata': {'name': 'foreign', 'uid': 'foreign'}}])
        self.api.put('node', node)
        self.api.put('namespace', {'metadata': {'name': 'kube-system', 'uid': 'cluster'}})
        self.api.put('namespace', {'metadata': {'name': product.NAMESPACE, 'uid': 'namespace'}})
        self.c.update(longhorn_action='uninstall', longhorn_uninstall_id='change-1')
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            values = Path(directory)/'values.json'
            values.with_name('installation.json').write_text(json.dumps({'cluster_uid': 'cluster', 'namespace_uid': 'namespace'}))
            product.reconcile(self.c, self.api, 'helm', values, {}, None)
        self.assertEqual(len(self.api.writes), 1)
        metadata = self.api.writes[0][1][2]['metadata']
        self.assertEqual(metadata['resourceVersion'], '1')
        self.assertEqual(metadata['labels'], {'node.longhorn.io/create-default-disk': None,
                                             'kubeauto.io/longhorn-client': None, 'kubeauto.io/component': None})
        self.assertEqual(metadata['annotations'], {'node.longhorn.io/default-disks-config': None})

    def test_bootstrap_cleanup_rechecks_uid_owner_and_version_on_conflict(self):
        expected = native('Node', 'worker')
        conflict = product.CommandError('kubectl', 1, 'Error from server (Conflict): stale')
        for fault in ('none', 'replacement', 'foreign', 'permission', 'exhausted'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                values = Path(directory)/'values.json'; values.with_name('installation.json').write_text('{}')
                first, second = copy.deepcopy(expected), copy.deepcopy(expected)
                second['metadata']['resourceVersion'] = 'fresh'
                if fault == 'replacement': second['metadata']['uid'] = 'foreign'
                if fault == 'foreign': second['metadata']['labels'] = {}
                denied = product.CommandError('kubectl', 1, 'Error from server (Forbidden): denied')
                errors = [denied] if fault == 'permission' else [conflict]*3 if fault == 'exhausted' else [conflict, None]
                with patch.object(product, 'check_installation_identity'), \
                        patch.object(self.api, 'get', side_effect=[{'items': [expected]}, first, second, second]), \
                        patch.object(self.api, 'patch', side_effect=errors) as apply, patch.object(product.time, 'sleep'):
                    if fault == 'none':
                        self.assertTrue(product.release_node_bootstrap(self.api, values))
                        self.assertEqual(apply.call_args.args[2]['metadata']['resourceVersion'], 'fresh')
                    else:
                        with self.assertRaises((ValueError, product.CommandError)):
                            product.release_node_bootstrap(self.api, values)
                    self.assertEqual(apply.call_count, 2 if fault == 'none' else 3 if fault == 'exhausted' else 1)

    def test_uninstall_blocks_retained_pv_and_unfinished_backup(self):
        self.api.listing('persistentvolumes', [{'spec': {'csi': {'driver': 'driver.longhorn.io'}}, 'status': {'phase': 'Released'}}])
        with self.assertRaisesRegex(ValueError, 'PVs remain'):
            product.empty_for_uninstall(self.api)
        self.api.listing('persistentvolumes', [])
        self.api.listing('backups.longhorn.io', [{'status': {'state': 'InProgress'}}], product.NAMESPACE)
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            product.empty_for_uninstall(self.api)


class RequestCompletionContracts(unittest.TestCase):
    def test_nullable_native_conditions_keep_waiting_then_complete(self):
        for kind, terminal in (('system-backup', 'Ready'), ('system-restore', 'Completed'),
                               ('snapshot', None), ('backup', 'Completed')):
            for conditions in (None, [], [{'type': 'Error', 'status': 'False'}]):
                with self.subTest(kind=kind, conditions=conditions):
                    fields = {'system-backup': {'volume_backup_policy': 'disabled'},
                              'system-restore': {'source': 'source'},
                              'snapshot': {'volume': 'volume'},
                              'backup': {'snapshot': 'snapshot', 'mode': 'full'}}
                    request = dict(kind=kind, name='request', **fields[kind])
                    obj = product.request_object(request)
                    obj['metadata']['uid'] = 'original'
                    pending = copy.deepcopy(obj)
                    pending['status'] = {'conditions': conditions, 'state': 'InProgress'}
                    ready = copy.deepcopy(obj)
                    ready['status'] = {'conditions': conditions, 'state': terminal,
                                       'readyToUse': True, 'url': 's3://verified/backup'}
                    api = FakeAPI()
                    with patch.object(api, 'get', side_effect=[obj, pending, ready]), \
                            patch.object(product, 'wait') as wait, redirect_stdout(StringIO()):
                        def poll(label, timeout, check):
                            self.assertFalse(check())
                            self.assertTrue(check())
                        wait.side_effect = poll
                        product.operation(configuration(), api, request)
                    self.assertEqual(api.writes, [])

    def test_native_request_error_or_replacement_still_stops(self):
        for state in ({'conditions': [{'type': 'Error', 'status': 'True'}], 'state': 'Ready'},
                      {'conditions': None, 'state': 'Error'}, {'conditions': None, 'error': 'failed'},
                      {'conditions': None, 'state': 'Unknown'}, None, 'replacement'):
            with self.subTest(state=state):
                request = {'kind': 'system-backup', 'name': 'request', 'volume_backup_policy': 'disabled'}
                obj = product.request_object(request)
                obj['metadata']['uid'] = 'original'
                current = copy.deepcopy(obj)
                current['status'] = state
                if state is None:
                    current = None
                elif state == 'replacement':
                    current['metadata']['uid'] = 'replacement'
                    current['status'] = {'conditions': None, 'state': 'Ready'}
                api = FakeAPI()
                with patch.object(api, 'get', side_effect=[obj, current]), \
                        patch.object(product, 'wait', side_effect=lambda label, timeout, check: check()):
                    with self.assertRaises(ValueError):
                        product.operation(configuration(), api, request)
                self.assertEqual(api.writes, [])


class RemovalContracts(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.ip = '192.168.122.212'
        self.node_name = 'worker-1'
        self.api.listing('nodes', [{'metadata': {'name': self.node_name}, 'status': {'addresses': [{'type': 'InternalIP', 'address': self.ip}]}}])
        self.api.listing('crds', [{'metadata': {'name': x + '.longhorn.io'}} for x in (
            'nodes', 'replicas', 'engines', 'instancemanagers', 'volumes', 'volumeattachments', 'backingimagemanagers')])
        self.api.listing('nodes.longhorn.io', [native('Node', self.node_name, {'allowScheduling': False, 'disks': {}})], '*')

    def test_empty_manager_does_not_permanently_block_evacuated_node(self):
        im = native('InstanceManager', 'im', {'nodeID': self.node_name})
        im['status'] = {'currentState': 'running', 'instanceEngines': {}, 'instanceReplicas': {}}
        self.api.listing('instancemanagers.longhorn.io', [im], '*')
        with redirect_stdout(StringIO()): product.guard(self.api, self.ip)
        self.assertEqual(self.api.writes, [])

    def test_active_manager_and_unknown_state_refused(self):
        for status in ({'currentState': 'unknown'}, {'currentState': 'running', 'instanceReplicas': {'r': {'status': {'state': 'stopped'}}}}):
            im = native('InstanceManager', 'im', {'nodeID': self.node_name}); im['status'] = status
            self.api.listing('instancemanagers.longhorn.io', [im], '*')
            with self.assertRaises(ValueError): product.guard(self.api, self.ip)

    def test_replica_and_engine_any_namespace_block(self):
        for plural in ('replicas', 'engines'):
            self.api.listing(plural + '.longhorn.io', [native('Replica', 'r', {'nodeID': self.node_name})], '*')
            with self.assertRaisesRegex(ValueError, 'replicas remain'):
                product.guard(self.api, self.ip)
            self.api.listing(plural + '.longhorn.io', [], '*')

    def test_volume_and_both_attachment_forms_block(self):
        examples = [('volumes.longhorn.io', native('Volume', 'v', {'nodeID': self.node_name}), '*'),
                    ('volumeattachments.longhorn.io', native('VolumeAttachment', 'va', {'attachmentTickets': {'t': {'nodeID': self.node_name}}}), '*'),
                    ('volumeattachments.storage.k8s.io', native('VolumeAttachment', 'va', {'attacher': 'driver.longhorn.io', 'nodeName': self.node_name}), '')]
        for kind, obj, namespace in examples:
            self.api.listing(kind, [obj], namespace)
            with self.assertRaisesRegex(ValueError, 'attach'):
                product.guard(self.api, self.ip)
            self.api.listing(kind, [], namespace)

    def test_backing_image_and_disks_block(self):
        self.api.listing('backingimagemanagers.longhorn.io', [native('BackingImageManager', 'bim', {'nodeID': self.node_name})], '*')
        with self.assertRaisesRegex(ValueError, 'backing image'):
            product.guard(self.api, self.ip)
        self.api.listing('backingimagemanagers.longhorn.io', [], '*')
        self.api.objects['nodes.longhorn.io', '', '*']['items'][0]['spec']['disks'] = {'d': {'path': '/data/longhorn'}}
        with self.assertRaisesRegex(ValueError, 'evacuated'):
            product.guard(self.api, self.ip)

    def test_cluster_teardown_refuses_crds_and_fails_closed_on_api_error(self):
        with self.assertRaisesRegex(ValueError, 'CRDs remain'):
            product.guard(self.api)
        with patch.object(self.api, 'get', side_effect=RuntimeError('api down')):
            with self.assertRaisesRegex(RuntimeError, 'api down'): product.guard(self.api)


class PinnedSourceContracts(unittest.TestCase):
    def test_chart_archives_and_consumed_api_schemas(self):
        requests = [dict(kind='snapshot', name='s', volume='v'), dict(kind='backup', name='b', snapshot='s', mode='full'),
                    dict(kind='system-backup', name='sb', volume_backup_policy='if-not-present'),
                    dict(kind='system-restore', name='sr', source='sb')]
        for version, digest in product.CHART_HASHES.items():
            chart = FILES / ('longhorn-' + version + '.tgz')
            self.assertEqual(hashlib.sha256(chart.read_bytes()).hexdigest(), digest)
            with tarfile.open(chart) as archive:
                crds = list(yaml.safe_load_all(archive.extractfile('longhorn/templates/crds.yaml').read().decode().replace('{{- include "longhorn.labels" . | nindent 4 }}', '')))
            by_kind = {x['spec']['names']['kind']: x['spec'] for x in crds if x}
            for request in requests:
                obj = product.request_object(request)
                schema = next(v['schema']['openAPIV3Schema'] for v in by_kind[obj['kind']]['versions'] if v['name'] == 'v1beta2')
                # Metadata is handled by Kubernetes; verify the consumed spec against the exact official version.
                Draft7Validator(schema['properties']['spec']).validate(obj['spec'])

    def test_image_inventory_equals_constants(self):
        tags = json.loads((FILES / 'images.json').read_text())
        constants = KubeConstant()
        for version in product.CHART_HASHES:
            self.assertEqual(tags[version], constants.longhorn_image_tags(version))
        evidence = json.loads((ROOT / 'tests/evidence/longhorn-images-20261021.json').read_text())
        self.assertEqual(tags['1.12.1']['longhorn-instance-manager'], 'v1.12.1-kubeauto.3')
        self.assertEqual(tags['1.12.1']['longhorn-manager'], 'v1.12.1-kubeauto.2')
        self.assertEqual(tags['1.12.0']['longhorn-manager'], 'v1.12.0')
        self.assertEqual(tags['1.12.0']['longhorn-instance-manager'], 'v1.12.0')
        self.assertEqual(len(evidence['images']), 24)
        self.assertEqual({row['key'] for row in evidence['images']}, set(constants.component_images['longhorn']))

    def test_snapshot_controller_exact_image_and_rbac_identity(self):
        objects = product.snapshot_objects(configuration())
        deployment = next(x for x in objects if x['kind'] == 'Deployment')
        pod = deployment['spec']['template']['spec']
        self.assertEqual(pod['containers'][0]['image'], configuration()['longhorn_image_prefix'] + '/snapshot-controller:v8.6.0')
        self.assertEqual(pod['serviceAccountName'], 'kubeauto-snapshot-controller')
        self.assertEqual(deployment['spec']['selector']['matchLabels'], deployment['spec']['template']['metadata']['labels'])
        sas = {(x['metadata']['name'], x['metadata'].get('namespace')) for x in objects if x['kind'] == 'ServiceAccount'}
        for obj in objects:
            for subject in obj.get('subjects', []):
                if subject['kind'] == 'ServiceAccount': self.assertIn((subject['name'], subject.get('namespace', obj['metadata'].get('namespace'))), sas)

    def test_helm_values_render(self):
        helm = shutil.which('helm') or ''
        if not Path(helm).is_file(): self.skipTest('Helm 3.19 required by local render gate')
        for version in product.CHART_HASHES:
            c = configuration(); c['longhorn_version'] = version
            with tempfile.TemporaryDirectory() as directory:
                values = Path(directory) / 'values.json'; values.write_text(json.dumps(product.chart_values(c)))
                manifest = subprocess.check_output([helm, 'template', 'longhorn', str(FILES / ('longhorn-' + version + '.tgz')),
                                                    '-n', product.NAMESPACE, '-f', str(values)], text=True)
            objects = [o for o in yaml.safe_load_all(manifest) if o]
            config = next(o for o in objects if o['metadata']['name'] == 'longhorn-default-setting')
            settings = yaml.safe_load(config['data']['default-setting.yaml'])
            self.assertFalse(settings['v2-data-engine'])
            self.assertFalse(settings['auto-salvage'])
            self.assertIn('control-plane:NoSchedule', settings['taint-toleration'])
            self.assertFalse(any(o['kind'] == 'StorageClass' for o in objects))
            manager = next(o for o in objects if o['kind'] == 'DaemonSet' and o['metadata']['name'] == 'longhorn-manager')
            self.assertEqual(manager['spec']['template']['spec']['nodeSelector'], {'kubeauto.io/longhorn-client': 'true'})
            tags = json.loads((FILES/'images.json').read_text())[version]
            manager_container = next(cn for cn in manager['spec']['template']['spec']['containers'] if cn['name'] == 'longhorn-manager')
            self.assertEqual(manager_container['image'], c['longhorn_image_prefix']+'/longhorn-manager:'+tags['longhorn-manager'])
            if version == '1.12.1':
                self.assertTrue(any(o['kind'] == 'NetworkPolicy' and o['metadata']['name'] == 'longhorn-manager' for o in objects))
            for obj in objects:
                if obj['kind'] in ('Deployment', 'DaemonSet', 'Job'):
                    for container in obj['spec']['template']['spec']['containers']:
                        self.assertTrue(container['image'].startswith(c['longhorn_image_prefix'] + '/'), container['image'])

    def test_postrenderer_adds_only_product_ownership(self):
        obj = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'x', 'labels': {'existing': 'yes'}}}
        stream = StringIO()
        with patch.object(sys, 'stdin', StringIO(yaml.safe_dump(obj))), redirect_stdout(stream): product.post_render()
        result = yaml.safe_load(stream.getvalue())
        self.assertEqual(result['metadata']['labels'], {'existing': 'yes', **product.OWNER})
        self.assertNotIn('spec', result)


class HostContracts(unittest.TestCase):
    def test_open_iscsi_2112_rejects_before_services_and_kernel_checks(self):
        for version in ('2.1.12', '2.1.12-1ubuntu1', '6.2.1.12-3.el9'):
            with self.subTest(version=version), patch.object(host.shutil, 'which', return_value='/usr/bin/dependency'), \
                    patch.object(host, 'output', side_effect=lambda argv: 'iscsiadm version ' + version
                                 if argv == ['iscsiadm', '--version'] else 'active') as output, \
                    patch.object(host.Path, 'exists', return_value=True), \
                    patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3)) as service:
                with self.assertRaisesRegex(ValueError, 'open-iscsi 2.1.12'):
                    host.clients()
                self.assertEqual(output.call_args_list, [unittest.mock.call(['iscsiadm', '--version'])])
                service.assert_not_called()

    def test_supported_iscsi_versions_continue_normal_host_validation(self):
        for version in ('2.1.11', '2.1.13', '6.2.1.9-1.el9', '2.1.120'):
            with self.subTest(version=version), patch.object(host.shutil, 'which', return_value='/usr/bin/dependency'), \
                    patch.object(host, 'output', side_effect=lambda argv: 'iscsiadm version ' + version
                                 if argv == ['iscsiadm', '--version'] else 'active') as output, \
                    patch.object(host.Path, 'exists', return_value=True), \
                    patch.object(host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3)) as service:
                host.clients()
                self.assertEqual(output.call_args_list, [unittest.mock.call(['iscsiadm', '--version']),
                                                        unittest.mock.call(['systemctl', 'is-active', 'iscsid'])])
                service.assert_called_once()

    def test_host_context_uses_cluster_and_unique_node_uids_even_during_offline_uninstall(self):
        c = configuration(); api = FakeAPI()
        api.put('namespace', native('Namespace', 'kube-system'))
        nodes = [native('Node', 'node-' + str(index)) for index, _ in enumerate(c['longhorn_storage_nodes'])]
        for node, address in zip(nodes, c['longhorn_storage_nodes']):
            node['status'] = {'addresses': [{'type': 'InternalIP', 'address': address}], 'conditions': [{'type': 'Ready', 'status': 'False'}]}
        api.listing('nodes', nodes)
        context = product.host_context(c, api)
        self.assertEqual(context['cluster_uid'], api.get('namespace', 'kube-system')['metadata']['uid'])
        self.assertEqual(context['node_uids'], {address: node['metadata']['uid'] for address, node in zip(c['longhorn_storage_nodes'], nodes)})
        self.assertFalse(api.writes)
        api.listing('nodes', nodes + [copy.deepcopy(nodes[0])])
        with self.assertRaisesRegex(ValueError, 'unique'): product.host_context(c, api)

    def test_native_uninstall_releases_only_identity_bound_mount_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = root / 'data'; data.mkdir()
            (data / 'customer-data').write_text('retain')
            receipt = root / 'receipt'; guard = root / 'guard'
            receipt.write_text(json.dumps({'path': str(data), 'uuid': 'original-disk', 'cluster_uid': 'cluster', 'node_uid': 'node'}))
            expected = '[Unit]\nRequiresMountsFor=' + str(data) + '\n[Service]\nExecStartPre=/usr/bin/mountpoint -q -- ' + str(data) + '\n'
            guard.write_text(expected)
            mounted = {'filesystems': [{'target': str(data), 'uuid': 'original-disk'}]}
            with patch.object(host, 'output', return_value=json.dumps(mounted)):
                self.assertTrue(host.release_mount_guard(str(data), receipt, guard, cluster_uid='cluster', node_uid='node', check_only=True))
                self.assertTrue(receipt.exists() and guard.exists())
                guard.write_text(expected + '# administrator change\n')
                with self.assertRaisesRegex(ValueError, 'modified mount guard'):
                    host.release_mount_guard(str(data), receipt, guard, cluster_uid='cluster', node_uid='node')
                self.assertTrue(receipt.exists())
                guard.write_text(expected)
                self.assertTrue(host.release_mount_guard(str(data), receipt, guard, cluster_uid='cluster', node_uid='node'))
                self.assertFalse(receipt.exists() or guard.exists())
                self.assertFalse(host.release_mount_guard(str(data), receipt, guard, cluster_uid='cluster', node_uid='node'))
            self.assertEqual((data / 'customer-data').read_text(), 'retain')

    def test_host_release_refuses_missing_receipt_replaced_disk_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipt = root / 'receipt'; guard = root / 'guard'
            guard.write_text('foreign')
            with self.assertRaisesRegex(ValueError, 'no filesystem receipt'):
                host.release_mount_guard('/data/longhorn', receipt, guard, cluster_uid='cluster', node_uid='node')
            receipt.write_text('{"path":"/data/longhorn","uuid":"old-disk","cluster_uid":"cluster","node_uid":"node"}')
            with patch.object(host, 'output', return_value=json.dumps({'filesystems': [
                    {'target': '/data/longhorn', 'uuid': 'replacement-disk'}]})):
                with self.assertRaisesRegex(ValueError, 'identity differs'):
                    host.release_mount_guard('/data/longhorn', receipt, guard, cluster_uid='cluster', node_uid='node')
            self.assertTrue(receipt.exists() and guard.exists())
            guard.unlink(); guard.symlink_to(receipt)
            with self.assertRaisesRegex(ValueError, 'non-file'):
                host.release_mount_guard('/data/longhorn', receipt, guard, cluster_uid='cluster', node_uid='node')
            self.assertTrue(receipt.exists())

    def test_host_release_follows_native_uninstall_and_checks_all_hosts_first(self):
        plays = yaml.safe_load((ROOT / 'playbooks/09.longhorn.yml').read_text())
        reconcile = next(i for i, play in enumerate(plays) if any(
            isinstance(role, dict) and role.get('longhorn_phase') == 'reconcile' for role in play.get('roles', [])))
        self.assertLess(reconcile, len(plays) - 1)
        release = plays[-1]
        self.assertTrue(release['any_errors_fatal'])
        self.assertFalse(release['gather_facts'])
        self.assertIn('--release check', release['tasks'][0]['ansible.builtin.script']['cmd'])
        scripts = [task for task in release['tasks'] if 'host-check.py --release' in task.get('ansible.builtin.script', {}).get('cmd', '')]
        self.assertEqual(len(scripts), 2)
        self.assertIn('--release apply', scripts[1]['ansible.builtin.script']['cmd'])
        for task in scripts:
            self.assertIn("longhorn_action | default('present') == 'uninstall'", task['when'])
            self.assertIn('inventory_hostname in longhorn_storage_nodes', task['when'])

    def test_root_and_symlink_fallback_rejected_without_commands(self):
        with patch.object(host, 'output', side_effect=AssertionError('command')):
            with self.assertRaisesRegex(ValueError, 'independent'):
                host.disk_receipt('/', 20)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / 'link').symlink_to(root, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'independent'): host.disk_receipt(str(root / 'link'), 20)

    def test_mount_fstab_uuid_and_existing_data_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'; root.mkdir()
            marker = Path(directory) / 'receipt.json'
            original_stat = Path.stat
            def independent(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path == root:
                    values = list(result); values[2] += 1
                    return type(result)(values)
                return result
            disk = {'target': str(root), 'source': '/dev/test-only', 'fstype': 'ext4', 'uuid': 'unit-uuid', 'options': 'rw,relatime'}
            persistent = {'target': str(root), 'source': '/dev/test-only', 'options': 'defaults'}
            def output(argv):
                if argv[0] == 'blkid': return 'unit-uuid'
                return json.dumps({'filesystems': [persistent if '--fstab' in argv else disk]})
            with patch.object(Path, 'stat', independent), patch.object(host, 'output', side_effect=output), \
                    patch.object(host.shutil, 'disk_usage', return_value=(100 * 1024**3, 1, 90 * 1024**3)):
                receipt = host.disk_receipt(str(root), 20, marker, cluster_uid='cluster', node_uid='node')
                self.assertEqual(receipt['uuid'], 'unit-uuid')
                (root / 'unknown-replica').touch()
                with self.assertRaisesRegex(ValueError, 'unknown data'): host.disk_receipt(str(root), 20, marker, cluster_uid='cluster', node_uid='node')
                marker.write_text(json.dumps(receipt))
                self.assertEqual(host.disk_receipt(str(root), 20, marker, cluster_uid='cluster', node_uid='node'), receipt)
                for old in (dict(receipt, cluster_uid='previous-cluster'), dict(receipt, node_uid='previous-node'),
                            {'path': str(root), 'uuid': 'unit-uuid'}):
                    marker.write_text(json.dumps(old))
                    with self.assertRaisesRegex(ValueError, 'another or unknown cluster/node'):
                        host.disk_receipt(str(root), 20, marker, cluster_uid='cluster', node_uid='node')
                    self.assertTrue((root / 'unknown-replica').exists())
                    self.assertEqual(json.loads(marker.read_text()), old)
                marker.write_text(json.dumps(dict(receipt, uuid='replaced-disk')))
                with self.assertRaisesRegex(ValueError, 'identity changed'): host.disk_receipt(str(root), 20, marker, cluster_uid='cluster', node_uid='node')
                persistent['options'] = 'noauto'
                with self.assertRaisesRegex(ValueError, 'fstab'): host.disk_receipt(str(root), 20, marker, cluster_uid='cluster', node_uid='node')
                persistent['options'] = 'defaults'; disk['options'] = 'ro'
                with self.assertRaisesRegex(ValueError, 'writable'): host.disk_receipt(str(root), 20, marker, cluster_uid='cluster', node_uid='node')

    def test_disk_identity_check_precedes_packages_and_both_guard_release_phases_are_bound(self):
        tasks = yaml.safe_load((ROOT / 'roles/longhorn/tasks/host.yml').read_text())
        probe = next(i for i, task in enumerate(tasks) if task['name'] == 'Reject foreign disk receipts before changing host dependencies')
        packages = next(i for i, task in enumerate(tasks) if task['name'].startswith('Install native'))
        self.assertLess(probe, packages)
        cmd = tasks[probe]['ansible.builtin.script']['cmd']
        self.assertIn('--disk-only', cmd)
        self.assertIn('--cluster-uid', cmd); self.assertIn('--node-uid', cmd)
        plays = yaml.safe_load((ROOT / 'playbooks/09.longhorn.yml').read_text())
        for task in plays[-1]['tasks']:
            if 'host-check.py --release' not in task.get('ansible.builtin.script', {}).get('cmd', ''):
                continue
            self.assertIn('--cluster-uid', task['ansible.builtin.script']['cmd'])
            self.assertIn('--node-uid', task['ansible.builtin.script']['cmd'])

    def test_missing_dependency_rejects(self):
        with patch.object(host.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'dependency'): host.clients()


class GrpcTLSContracts(unittest.TestCase):
    @staticmethod
    def material(failure='none'):
        from datetime import datetime, timedelta, timezone
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
        now = datetime.now(timezone.utc)
        key = ec.generate_private_key(ec.SECP256R1())
        ca_key = ec.generate_private_key(ec.SECP256R1())
        issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'contract CA')])
        ca = (x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=2))
              .not_valid_after(now+timedelta(days=7)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
              .sign(ca_key, hashes.SHA256()))
        names = [host+suffix for host in ('longhorn-backend', 'longhorn-frontend', 'longhorn-engine-manager',
                 'longhorn-replica-manager', 'longhorn-csi') for suffix in ('', '.longhorn-system', '.longhorn-system.svc')]
        if failure == 'dns': names.remove('longhorn-csi.longhorn-system.svc')
        sans = [x509.DNSName(name) for name in names]
        if failure != 'ip': sans.append(x509.IPAddress(ipaddress.ip_address('127.0.0.1')))
        usages = [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
        if failure == 'server-purpose': usages.remove(ExtendedKeyUsageOID.SERVER_AUTH)
        if failure == 'client-purpose': usages.remove(ExtendedKeyUsageOID.CLIENT_AUTH)
        start = now+timedelta(hours=1) if failure == 'future' else now-timedelta(days=1)
        end = now+timedelta(days=3)
        if failure == 'expired': end = now-timedelta(hours=1)
        if failure == 'near-expiry': end = now+timedelta(hours=12)
        signer = ec.generate_private_key(ec.SECP256R1()) if failure == 'ca' else ca_key
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'longhorn-backend')]))
                .issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(end).add_extension(x509.BasicConstraints(ca=False,path_length=None),True)
                .add_extension(x509.SubjectAlternativeName(sans),False).add_extension(x509.ExtendedKeyUsage(usages),False)
                .sign(signer, hashes.SHA256()))
        if failure == 'key': key = ec.generate_private_key(ec.SECP256R1())
        data = {'ca.crt': ca.public_bytes(serialization.Encoding.PEM), 'tls.crt': cert.public_bytes(serialization.Encoding.PEM),
                'tls.key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                    serialization.BestAvailableEncryption(b'not-a-customer-secret') if failure == 'encrypted-key' else serialization.NoEncryption())}
        if failure == 'malformed': data['tls.crt'] = b'not a PEM certificate'
        return data

    def test_actual_mutual_tls_needs_no_executable_or_network(self):
        import base64
        import os
        from types import SimpleNamespace
        data = self.material()
        config = configuration(); config['longhorn_grpc_tls_required'] = True
        api = SimpleNamespace(get=lambda *_: {'data': {k: base64.b64encode(v).decode() for k,v in data.items()}})
        with patch.object(product, 'run', side_effect=FileNotFoundError('openssl')), \
             patch.dict(os.environ, {'PATH': ''}), patch('socket.socket', side_effect=AssertionError('network')):
            product.check_secrets(config, api)

    def test_current_rsa_gate_credentials_pass_real_tls13_verification(self):
        spec = importlib.util.spec_from_file_location('tls_fixture_contract', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
        with tempfile.TemporaryDirectory() as directory:
            for name, data in lab.tls_fixture().items(): (Path(directory)/name).write_text(data)
            product.verify_grpc_tls(directory)

    def test_real_certificate_failures_reject_without_relaxing_validation(self):
        import ssl
        for failure in ('ca', 'key', 'dns', 'ip', 'server-purpose', 'client-purpose', 'expired',
                        'future', 'near-expiry', 'encrypted-key', 'malformed'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                for name, data in self.material(failure).items(): (Path(directory)/name).write_bytes(data)
                with self.assertRaises((ssl.SSLError, ValueError)):
                    product.verify_grpc_tls(directory)


if __name__ == '__main__':
    unittest.main()
