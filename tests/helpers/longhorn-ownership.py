"""Qualify ownership refusal without deploying a second storage system."""
import base64
import gzip
import hashlib
import json
from pathlib import Path

import yaml

CSI_REFUSAL = 'existing Longhorn CSI driver has no local installation receipt; adoption is not supported'


def preinstall_core(state):
    """Accept a native retirement or a clean core with no installation history."""
    return (state.get('native_uninstalled') is True or
            (state.get('clean_start_verified') is True and state.get('healthy') is False and
             not any(key in state for key in
                     ('native_uninstalled', 'namespace_uid', 'tls_uid', 'encryption_uid'))))


def identity(obj):
    if not obj:
        return None
    meta = obj['metadata']
    return {'uid': meta['uid'], 'labels': meta.get('labels', {}),
            'annotations': meta.get('annotations', {}), 'spec': obj.get('spec', {})}


def installation_refusal(lab, relay_pid, refusal, marker):
    path = lab.DIRECTORY / 'config.yml'
    receipt = lab.DIRECTORY / 'longhorn-request-input.json'
    if path.is_symlink() or receipt.exists() or receipt.is_symlink():
        raise ValueError('installation refusal requires an unchanged product input without a pending request')
    original = path.read_text()
    config = yaml.safe_load(original)
    if (str(config['longhorn_install']).lower() not in ('no', 'false') or
            config['longhorn_action'] != 'present' or config['longhorn_requests']):
        raise ValueError('installation refusal requires the original disabled, empty post-uninstall configuration')
    config['longhorn_install'] = 'yes'
    lab.write_private(receipt, {'run_id': lab.owner()['run_id'],
        'original': original, 'desired': json.dumps(config, sort_keys=True)})
    with lab.scoped_cleanup(lab.restore_request_input):
        lab.write_private(path, config)
        lab.supervise_core(relay_pid, 'longhorn-setup', expected_refusal=refusal)
    if path.read_text() != original or receipt.exists():
        raise ValueError('installation refusal did not restore the original product input')
    print(marker, flush=True)


class CSIProbe:
    """Only the exact returned UID authorizes removal of this canary."""
    def __init__(self, lab):
        self.lab = lab
        self.path = lab.DIRECTORY / 'longhorn-ownership-csi.json'

    def receipt(self):
        if self.path.is_symlink():
            raise ValueError('symlink CSI ownership journal')
        row = json.loads(self.path.read_text())
        state = self.lab.owner()
        if any(row.get(key) != state[key] for key in ('run_id', 'cluster_uid')):
            raise ValueError('foreign CSI ownership journal')
        return row

    def current(self):
        return self.lab.get('csidriver', 'driver.longhorn.io')

    def cleanup(self):
        if not self.path.exists() and not self.path.is_symlink():
            return
        row = self.receipt()
        if not row.get('created'):
            raise ValueError('CSI canary creation lacks its returned UID; retain the uncertain intent')
        current = self.current()
        if current:
            if identity(current) != row['created']:
                raise ValueError('CSI ownership canary was replaced, changed or lacks its returned UID')
            self.lab.remove(current)
            self.lab.wait('ownership-csi-canary-removed', 60, lambda: not self.current())
        self.path.unlink()
        print('LONGHORN_OWNERSHIP_CSI_CANARY_CLEAN_PASS', flush=True)

    def product_refusal(self, relay_pid):
        installation_refusal(self.lab, relay_pid, CSI_REFUSAL,
                             'LONGHORN_OWNERSHIP_CSI_PRODUCT_REFUSAL_VERIFIED')

    def qualify(self, relay_pid):
        if type(relay_pid) is not int or relay_pid <= 0:
            raise ValueError('CSI product qualification requires the live runner relay')
        self.run(relay_pid=relay_pid)

    def run(self, *, relay_pid=None):
        self.lab.core_identity()
        state = self.lab.owner()
        values = self.lab.DIRECTORY / 'yml/longhorn/values.json'
        if (self.path.exists() or self.path.is_symlink() or
                not preinstall_core(state) or
                values.with_name('installation.json').exists() or
                values.with_name('installation.json').is_symlink() or
                self.lab.get('namespace', 'longhorn-system') or self.current() or
                any(obj['metadata']['name'].endswith('.longhorn.io')
                    for obj in self.lab.get('crds')['items'])):
            raise ValueError('CSI ownership probe requires the verified post-uninstall core or clean never-installed core')
        releases = json.loads(self.lab.execute([self.lab.BASE / 'extra-bin/helm', 'list',
            '--all', '--all-namespaces', '-o', 'json']).stdout)
        if any(row['chart'].startswith('longhorn-') for row in releases):
            raise ValueError('CSI identity probe requires no existing Longhorn Helm release')
        product = self.lab.load_helper('../../roles/longhorn/files/longhorn.py')
        shared = self.lab.shared_snapshot_identity()
        row = {key: state[key] for key in ('run_id', 'cluster_uid')}
        row['desired'] = {'apiVersion': 'storage.k8s.io/v1', 'kind': 'CSIDriver',
            'metadata': {'name': 'driver.longhorn.io',
                         'annotations': {'kubeauto.io/test-run': state['run_id']}},
            'spec': {'attachRequired': True, 'podInfoOnMount': True,
                     'volumeLifecycleModes': ['Persistent']}}
        self.lab.write_private(self.path, row)
        with self.lab.scoped_cleanup(self.cleanup):
            created = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json',
                stdin=json.dumps(row['desired'])).stdout)
            if created.get('kind') != 'CSIDriver' or created['metadata']['name'] != 'driver.longhorn.io':
                raise ValueError('CSI canary creation returned another object')
            row['created'] = identity(created)
            self.lab.write_private(self.path, row)
            # The original-gap probe calls only the read-only identity
            # subcheck. Qualification instead uses config+kubecli and requires
            # the fixed preflight to refuse before any host/deployer work.
            if relay_pid is not None:
                self.product_refusal(relay_pid)
            else:
                try:
                    product.check_installation_identity(product.API(str(self.lab.DIRECTORY / 'kubectl.kubeconfig')),
                                                        values, None)
                except ValueError as error:
                    if str(error) != CSI_REFUSAL:
                        raise
                    print('LONGHORN_OWNERSHIP_CSI_READONLY_REFUSAL_OBSERVED', flush=True)
                else:
                    print('LONGHORN_OWNERSHIP_CSI_GAP_OBSERVED ' + json.dumps(row['created'], sort_keys=True), flush=True)
            if identity(self.current()) != row['created']:
                raise ValueError('CSI canary changed during the owning product check')
        if self.lab.shared_snapshot_identity() != shared:
            raise ValueError('CSI identity probe changed shared snapshot resources')
        self.lab.core_identity()
        print('LONGHORN_OWNERSHIP_CSI_' + ('REFUSAL' if relay_pid is not None else 'PROBE') + '_BUSINESS_PASS', flush=True)


class PreinstallProbe:
    """One original-UID canary at a time; no second Longhorn deployment."""
    REFUSALS = {
        'namespace': 'foreign resource: longhorn-system',
        'release': 'another Longhorn Helm installation exists',
        'crd': 'existing Longhorn release or CRDs have no local installation receipt; adoption is not supported',
    }

    def __init__(self, lab):
        self.lab = lab
        self.path = lab.DIRECTORY / 'longhorn-ownership-preinstall.json'

    def desired(self, scenario):
        run_id = self.lab.owner()['run_id']
        metadata = {'annotations': {'kubeauto.io/test-run': run_id}}
        if scenario == 'namespace':
            metadata['name'] = 'longhorn-system'
            return {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': metadata}
        if scenario == 'crd':
            metadata['name'] = 'kubeautoownershipprobes.longhorn.io'
            return {'apiVersion': 'apiextensions.k8s.io/v1', 'kind': 'CustomResourceDefinition',
                    'metadata': metadata, 'spec': {'group': 'longhorn.io', 'scope': 'Namespaced',
                    'names': {'plural': 'kubeautoownershipprobes', 'singular': 'kubeautoownershipprobe',
                              'kind': 'KubeautoOwnershipProbe'},
                    'versions': [{'name': 'v1', 'served': True, 'storage': True,
                                  'schema': {'openAPIV3Schema': {'type': 'object'}}}]}}
        if scenario != 'release':
            raise ValueError('unknown preinstall ownership scenario')
        # Helm 3.19.0 Secrets driver: JSON -> gzip -> base64; Kubernetes
        # Secret.data adds the outer base64. No templates, hooks or workloads.
        release_name = 'lh-owner-' + run_id
        release = {'name': release_name, 'namespace': 'kube-system', 'version': 1,
                   'info': {'status': 'deployed', 'description': 'Ownership metadata fixture only'},
                   'chart': {'metadata': {'name': 'longhorn', 'version': '1.12.1',
                                          'appVersion': '1.12.1', 'apiVersion': 'v2'}},
                   'config': {}, 'manifest': ''}
        encoded = base64.b64encode(gzip.compress(json.dumps(release).encode(), mtime=0))
        metadata.update(name='sh.helm.release.v1.' + release_name + '.v1', namespace='kube-system',
                        labels={'owner': 'helm', 'name': release_name, 'status': 'deployed', 'version': '1'})
        return {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata,
                'type': 'helm.sh/release.v1', 'data': {'release': base64.b64encode(encoded).decode()}}

    def current(self, row):
        desired = row['desired']; meta = desired['metadata']
        return self.lab.get(desired['kind'], meta['name'], meta.get('namespace', ''))

    @staticmethod
    def binding(obj):
        return dict(identity(obj), data=obj.get('data', {}), type=obj.get('type', '')) if obj else None

    def receipt(self):
        if self.path.is_symlink():
            raise ValueError('symlink preinstall ownership journal')
        row = json.loads(self.path.read_text()); state = self.lab.owner()
        if any(row.get(key) != state[key] for key in ('run_id', 'cluster_uid')):
            raise ValueError('foreign preinstall ownership journal')
        if row.get('scenario') not in self.REFUSALS or row.get('desired') != self.desired(row['scenario']):
            raise ValueError('preinstall canary declaration changed')
        return row

    def cleanup(self):
        if not self.path.exists() and not self.path.is_symlink():
            return
        row = self.receipt()
        if not row.get('created'):
            raise ValueError('preinstall canary lacks returned UID; retain uncertain intent')
        current = self.current(row)
        if current:
            if self.binding(current) != row['created']:
                raise ValueError('preinstall canary was replaced or changed')
            if row['scenario'] == 'namespace':
                if not row.get('defaults'):
                    raise ValueError('preinstall namespace lacks original default identities')
                self.lab.check_business_graph({'namespace': 'longhorn-system',
                    'namespace_uid': row['created']['uid'], 'defaults': row['defaults'], 'objects': {}})
            if row['scenario'] == 'crd':
                if self.lab.get(row['desired']['metadata']['name'], namespace='*')['items']:
                    raise ValueError('foreign custom resources prevent canary CRD deletion')
            self.lab.remove(current)
            self.lab.wait('ownership-preinstall-canary-removed', 90, lambda: not self.current(row))
        self.path.unlink()
        print('LONGHORN_OWNERSHIP_PREINSTALL_CANARY_CLEAN_PASS scenario=' + row['scenario'], flush=True)

    def pristine(self):
        self.lab.core_identity()
        values = self.lab.DIRECTORY / 'yml/longhorn/values.json'
        if (self.path.exists() or self.path.is_symlink() or
                (self.lab.DIRECTORY / 'longhorn-ownership-csi.json').exists() or
                (self.lab.DIRECTORY / 'longhorn-ownership-csi.json').is_symlink() or
                not preinstall_core(self.lab.owner()) or
                values.with_name('installation.json').exists() or
                values.with_name('installation.json').is_symlink() or
                self.lab.get('namespace', 'longhorn-system') or
                self.lab.get('csidriver', 'driver.longhorn.io') or
                any(obj['metadata']['name'].endswith('.longhorn.io')
                    for obj in self.lab.get('crds')['items']) or
                any(row['chart'].startswith('longhorn-') for row in self.releases())):
            raise ValueError('preinstall ownership requires the verified post-uninstall core or clean never-installed core')

    def releases(self):
        return json.loads(self.lab.execute([self.lab.BASE / 'extra-bin/helm', 'list',
            '--all', '--all-namespaces', '-o', 'json']).stdout)

    def qualify(self, relay_pid):
        if type(relay_pid) is not int or relay_pid <= 0:
            raise ValueError('preinstall qualification requires the live runner relay')
        shared = self.lab.shared_snapshot_identity()
        for scenario, refusal in self.REFUSALS.items():
            self.pristine()
            row = {key: self.lab.owner()[key] for key in ('run_id', 'cluster_uid')}
            row.update(scenario=scenario, desired=self.desired(scenario))
            self.lab.write_private(self.path, row)
            with self.lab.scoped_cleanup(self.cleanup):
                obj = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json',
                                                 stdin=json.dumps(row['desired'])).stdout)
                desired = row['desired']; meta = obj.get('metadata', {})
                if (obj.get('kind') != desired['kind'] or not meta.get('uid') or
                        any(meta.get(key, '') != desired['metadata'].get(key, '') for key in ('name', 'namespace'))):
                    raise ValueError('preinstall canary create returned another object')
                row['created'] = self.binding(obj); self.lab.write_private(self.path, row)
                if scenario == 'namespace':
                    row['defaults'] = self.lab.wait('ownership-namespace-defaults', 60,
                        lambda: self.lab.namespace_defaults('longhorn-system'))
                    self.lab.write_private(self.path, row)
                elif scenario == 'crd':
                    self.lab.wait('ownership-canary-crd-established', 60, lambda: any(
                        c.get('type') == 'Established' and c.get('status') == 'True'
                        for c in self.current(row).get('status', {}).get('conditions', [])))
                else:
                    expected = 'lh-owner-' + row['run_id']
                    matches = [r for r in self.releases() if r['name'] == expected]
                    if (len(matches) != 1 or matches[0]['namespace'] != 'kube-system' or
                            matches[0]['chart'] != 'longhorn-1.12.1' or
                            matches[0]['app_version'] != '1.12.1' or matches[0]['status'] != 'deployed'):
                        raise ValueError('real Helm list did not recognize the exact metadata canary')
                installation_refusal(self.lab, relay_pid, refusal,
                    'LONGHORN_OWNERSHIP_PREINSTALL_PRODUCT_REFUSAL_VERIFIED scenario=' + scenario)
                if self.binding(self.current(row)) != row['created']:
                    raise ValueError('product refusal changed the preinstall canary')
            self.pristine()
            if self.lab.shared_snapshot_identity() != shared:
                raise ValueError('preinstall qualification changed shared snapshot identities')
        print('LONGHORN_OWNERSHIP_PREINSTALL_REFUSAL_BUSINESS_PASS', flush=True)


class InstalledClassProbe:
    """A non-default, non-provisioning class, protected by UID and consumers."""
    def __init__(self, lab):
        self.lab = lab
        self.path = lab.DIRECTORY / 'longhorn-ownership-class.json'

    def desired(self):
        return {'apiVersion': 'storage.k8s.io/v1', 'kind': 'StorageClass',
                'metadata': {'name': 'longhorn-owner-' + self.lab.owner()['run_id'],
                             'annotations': {'kubeauto.io/test-run': self.lab.owner()['run_id']}},
                'provisioner': 'kubeauto.invalid/no-provisioner', 'reclaimPolicy': 'Retain',
                'volumeBindingMode': 'WaitForFirstConsumer', 'allowVolumeExpansion': False}

    @staticmethod
    def binding(obj):
        if not obj:
            return None
        return dict(identity(obj), **{key: obj.get(key) for key in (
            'provisioner', 'parameters', 'reclaimPolicy', 'volumeBindingMode',
            'allowVolumeExpansion', 'mountOptions', 'allowedTopologies')})

    def current(self):
        return self.lab.get('storageclass', self.desired()['metadata']['name'])

    def cleanup(self):
        if not self.path.exists() and not self.path.is_symlink():
            return
        if self.path.is_symlink():
            raise ValueError('symlink installed class ownership journal')
        row = json.loads(self.path.read_text()); state = self.lab.owner()
        if (any(row.get(key) != state[key] for key in ('run_id', 'cluster_uid')) or
                row.get('desired') != self.desired() or not row.get('created')):
            raise ValueError('installed class canary lacks original ownership; retain intent')
        current = self.current()
        if current:
            if self.binding(current) != row['created']:
                raise ValueError('installed class canary was replaced or changed')
            name = row['desired']['metadata']['name']
            for kind, namespace in (('pv', ''), ('pvc', '*')):
                if any(obj.get('spec', {}).get('storageClassName') == name
                       for obj in self.lab.get(kind, namespace=namespace)['items']):
                    raise ValueError('installed class canary has a PV or PVC consumer')
            self.lab.remove(current)
            self.lab.wait('ownership-installed-class-removed', 60, lambda: not self.current())
        self.path.unlink()
        print('LONGHORN_OWNERSHIP_CLASS_CANARY_CLEAN_PASS', flush=True)

    def qualify(self, relay_pid):
        if type(relay_pid) is not int or relay_pid <= 0:
            raise ValueError('installed class qualification requires the live runner relay')
        self.lab.installation_health()
        if (self.path.exists() or self.path.is_symlink() or self.current() or
                any((self.lab.DIRECTORY / name).exists() or (self.lab.DIRECTORY / name).is_symlink()
                    for name in ('longhorn-request-input.json', 'longhorn-install-business.json',
                                 'longhorn-ownership-csi.json', 'longhorn-ownership-preinstall.json',
                                 'longhorn-ownership-controller.json', 'longhorn-ownership-request.json'))):
            raise ValueError('installed class qualification requires no pending ownership or business fixture')
        original = (self.lab.DIRECTORY / 'config.yml').read_bytes()
        config = yaml.safe_load(original)
        before = self.lab.idempotence_snapshot('longhorn-system')
        classes = self.lab.class_identity(); shared = self.lab.shared_snapshot_identity()
        row = {key: self.lab.owner()[key] for key in ('run_id', 'cluster_uid')}
        row['desired'] = self.desired()
        if any(sc['name'] == row['desired']['metadata']['name'] for sc in config['longhorn_storage_classes']):
            raise ValueError('installed class canary collides with an existing product declaration')
        self.lab.write_private(self.path, row)
        with self.lab.scoped_cleanup(self.cleanup):
            obj = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json',
                                            stdin=json.dumps(row['desired'])).stdout)
            if (obj.get('kind') != 'StorageClass' or not obj.get('metadata', {}).get('uid') or
                    obj['metadata'].get('name') != row['desired']['metadata']['name']):
                raise ValueError('installed class canary returned another object')
            row['created'] = self.binding(obj); self.lab.write_private(self.path, row)
            declared = dict(config['longhorn_storage_classes'][0], name=obj['metadata']['name'], default=False)
            self.lab.run_requests(relay_pid, [], storage_classes=config['longhorn_storage_classes'] + [declared],
                                  expected_refusal='foreign resource: ' + obj['metadata']['name'])
            if self.binding(self.current()) != row['created']:
                raise ValueError('product refusal changed the foreign class canary')
            print('LONGHORN_OWNERSHIP_CLASS_PRODUCT_REFUSAL_VERIFIED', flush=True)
        if ((self.lab.DIRECTORY / 'config.yml').read_bytes() != original or
                self.lab.class_identity() != classes or self.lab.idempotence_snapshot('longhorn-system') != before or
                self.lab.shared_snapshot_identity() != shared):
            raise ValueError('installed class refusal changed the original configuration or baseline')
        self.lab.installation_health()
        print('LONGHORN_OWNERSHIP_CLASS_REFUSAL_BUSINESS_PASS', flush=True)


class InstalledControllerProbe:
    """A paused zero-replica declaration; no second controller ever runs."""
    def __init__(self, lab):
        self.lab = lab
        self.path = lab.DIRECTORY / 'longhorn-ownership-controller.json'
        self.name = 'longhorn-owner-' + lab.owner()['run_id']

    def desired(self, config):
        labels = {'kubeauto.io/ownership-canary': self.lab.owner()['run_id']}
        return {'apiVersion': 'apps/v1', 'kind': 'Deployment',
                'metadata': {'name': self.name, 'namespace': 'kube-system', 'labels': labels},
                'spec': {'replicas': 0, 'paused': True, 'selector': {'matchLabels': labels},
                         'template': {'metadata': {'labels': labels}, 'spec': {'containers': [
                             {'name': 'snapshot-controller', 'image':
                              config['longhorn_image_prefix'] + '/snapshot-controller:v8.6.0'}]}}}}

    def current(self):
        return self.lab.get('deployment', self.name, 'kube-system')

    def cleanup(self):
        if not self.path.exists() and not self.path.is_symlink():
            return
        if self.path.is_symlink():
            raise ValueError('symlink controller ownership journal')
        row = json.loads(self.path.read_text()); state = self.lab.owner()
        config = yaml.safe_load((self.lab.DIRECTORY / 'config.yml').read_text())
        namespace = self.lab.get('namespace', 'kube-system')
        if (any(row.get(key) != state[key] for key in ('run_id', 'cluster_uid')) or
                row.get('desired') != self.desired(config) or not row.get('created') or
                not namespace or namespace['metadata']['uid'] != row.get('namespace_uid')):
            raise ValueError('controller canary lacks original ownership; retain intent')
        current = self.current()
        if current:
            if identity(current) != row['created']:
                raise ValueError('controller canary was replaced or changed')
            labels = row['desired']['spec']['selector']['matchLabels']
            for kind in ('replicaset', 'pod'):
                for obj in self.lab.get(kind, namespace='kube-system')['items']:
                    meta = obj['metadata']
                    if (any(ref.get('uid') == row['created']['uid'] for ref in meta.get('ownerReferences', [])) or
                            all(meta.get('labels', {}).get(k) == v for k, v in labels.items())):
                        raise ValueError('paused controller canary has a ReplicaSet or Pod; retain journal')
            self.lab.remove(current)
            self.lab.wait('ownership-controller-removed', 60, lambda: not self.current())
        self.path.unlink()
        print('LONGHORN_OWNERSHIP_CONTROLLER_CANARY_CLEAN_PASS', flush=True)

    def qualify(self, relay_pid):
        if type(relay_pid) is not int or relay_pid <= 0:
            raise ValueError('controller qualification requires the live runner relay')
        self.lab.installation_health()
        pending = ('longhorn-request-input.json', 'longhorn-install-business.json',
                   'longhorn-ownership-csi.json', 'longhorn-ownership-preinstall.json',
                   'longhorn-ownership-class.json', 'longhorn-ownership-request.json', self.path.name)
        if self.current() or any((self.lab.DIRECTORY / name).exists() or
                                (self.lab.DIRECTORY / name).is_symlink() for name in pending):
            raise ValueError('controller qualification requires no pending ownership or business fixture')
        original = (self.lab.DIRECTORY / 'config.yml').read_bytes()
        config = yaml.safe_load(original)
        if config['longhorn_snapshot_controller'] != 'managed':
            raise ValueError('controller qualification requires the original managed snapshot configuration')
        namespace = self.lab.get('namespace', 'kube-system')
        if not namespace:
            raise ValueError('controller qualification requires the original kube-system namespace')
        before = self.lab.idempotence_snapshot('kube-system')
        shared = self.lab.shared_snapshot_identity(); classes = self.lab.class_identity()
        row = {key: self.lab.owner()[key] for key in ('run_id', 'cluster_uid')}
        row.update(desired=self.desired(config), namespace_uid=namespace['metadata']['uid'])
        self.lab.write_private(self.path, row)
        with self.lab.scoped_cleanup(self.cleanup):
            obj = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json',
                                            stdin=json.dumps(row['desired'])).stdout)
            if (obj.get('kind') != 'Deployment' or not obj.get('metadata', {}).get('uid') or
                    obj['metadata'].get('name') != self.name or
                    obj['metadata'].get('namespace') != 'kube-system'):
                raise ValueError('controller canary creation returned another object')
            row['created'] = identity(obj); self.lab.write_private(self.path, row)
            self.lab.run_requests(relay_pid, [], expected_refusal='refusing a second or foreign snapshot controller')
            if identity(self.current()) != row['created']:
                raise ValueError('product refusal changed the foreign controller declaration')
            print('LONGHORN_OWNERSHIP_CONTROLLER_PRODUCT_REFUSAL_VERIFIED', flush=True)
        if ((self.lab.DIRECTORY / 'config.yml').read_bytes() != original or
                self.lab.idempotence_snapshot('kube-system') != before or
                self.lab.shared_snapshot_identity() != shared or self.lab.class_identity() != classes):
            raise ValueError('controller refusal changed original configuration or installation baseline')
        self.lab.installation_health()
        print('LONGHORN_OWNERSHIP_CONTROLLER_REFUSAL_BUSINESS_PASS', flush=True)


class InstalledRequestProbe:
    """Real native snapshots on the runner-owned volume, retired with their source."""
    def __init__(self, lab):
        self.lab = lab
        self.path = lab.DIRECTORY / 'longhorn-ownership-request.json'

    def desired(self, journal, scenario):
        if scenario not in ('foreign', 'immutable'):
            raise ValueError('unknown request ownership scenario')
        product = self.lab.load_helper('../../roles/longhorn/files/longhorn.py')
        request = {'kind': 'snapshot', 'name': 'owner-' + scenario + '-' + self.lab.owner()['run_id'],
                   'volume': journal['volume']}
        obj = product.request_object(request)
        if scenario == 'foreign':
            obj['metadata']['labels'] = {}
        obj['metadata']['annotations'] = {'kubeauto.io/test-run': self.lab.owner()['run_id']}
        if scenario == 'immutable':
            obj['metadata']['annotations']['kubeauto.io/request-sha256'] = '0' * 64
        return request, obj

    @staticmethod
    def binding(obj):
        if not obj:
            return None
        meta = obj['metadata']
        return dict(identity(obj), ownerReferences=meta.get('ownerReferences', []),
                    finalizers=meta.get('finalizers', []))

    def source(self, journal):
        current = self.lab.get('volumes.longhorn.io', journal['volume'], 'longhorn-system')
        namespace = self.lab.get('namespace', 'longhorn-system')
        if (not current or current['metadata']['uid'] != journal['volume_uid'] or
                current.get('spec', {}).get('dataEngine', 'v1') != 'v1' or
                not namespace or namespace['metadata']['uid'] != self.lab.owner()['namespace_uid']):
            raise ValueError('request canary source volume or installation identity changed')
        return current

    def current(self, row):
        return self.lab.get('snapshots.longhorn.io', row['desired']['metadata']['name'], 'longhorn-system')

    def engine_snapshots(self, journal, *, require_running=False):
        engines = [obj for obj in self.lab.get('engines.longhorn.io', namespace='longhorn-system')['items']
                   if obj.get('spec', {}).get('volumeName') == journal['volume']]
        if len(engines) != 1 or (require_running and engines[0].get('status', {}).get('currentState') != 'running'):
            raise ValueError('request ownership qualification requires one source engine with the expected state')
        return {'uid': engines[0]['metadata']['uid'],
                'snapshots': sorted(engines[0].get('status', {}).get('snapshots', {}))}

    def check_consumers(self, journal, name):
        if any(obj.get('spec', {}).get('snapshotName') == name for obj in
               self.lab.get('backups.longhorn.io', namespace='longhorn-system')['items']):
            raise ValueError('request canary has a backup consumer')
        source = 'snap://' + journal['volume'] + '/' + name
        if any(source in (obj.get('spec', {}).get('source', {}).get('snapshotHandle'),
                          obj.get('status', {}).get('snapshotHandle')) for obj in
               self.lab.get('volumesnapshotcontents.snapshot.storage.k8s.io')['items']):
            raise ValueError('request canary has a CSI snapshot consumer')
        if any(obj.get('spec', {}).get('dataSource') == source for obj in
               self.lab.get('volumes.longhorn.io', namespace='longhorn-system')['items']):
            raise ValueError('request canary has a clone consumer')

    def recover_archived_creation(self, row, journal, current):
        """Transfer only the product-created Snapshot proven by this failed run.

        The old canary UID remains immutable in the archived evidence. This
        exception grants no ownership for another run or replacement object.
        Native business cleanup retains its normal UID and consumer checks.
        """
        proof_path = (Path(__file__).resolve().parents[1] /
                      'evidence/longhorn-ownership-request-fixture-failure-20261020.json')
        proof = json.loads(proof_path.read_text())['recovery']
        state = self.lab.owner()
        if (row != proof['canary'] or self.binding(current) != proof['current'] or
                any(state.get(key) != value for key, value in proof['owner'].items())):
            raise ValueError('request canary was replaced or changed')
        expected = dict(proof['business'])
        name = proof['request']['name']
        native = {'request': proof['request'], 'uid': proof['current']['uid']}
        resumed = dict(expected, native_snapshots={name: native})
        if journal != expected and journal != resumed:
            raise ValueError('archived request recovery business identity changed')
        for path, digest in proof['source_sha256'].items():
            if path.startswith('roles/longhorn/'):
                if hashlib.sha256((self.lab.BASE / path).read_bytes()).hexdigest() != digest:
                    raise ValueError('archived request production source changed')
        observation = proof['product_observation']
        private_path = self.lab.DIRECTORY / observation['name']
        if (private_path.is_symlink() or
                hashlib.sha256(private_path.read_bytes()).hexdigest() != observation['sha256'] or
                json.loads(private_path.read_text()).get('rc') != 0):
            raise ValueError('archived successful request observation changed')
        product = self.lab.load_helper('../../roles/longhorn/files/longhorn.py')
        product.check_request(current, product.request_object(proof['request']))
        self.source(journal)
        engine = self.engine_snapshots(journal)
        if engine['uid'] != proof['engine']['uid'] or name not in engine['snapshots']:
            raise ValueError('archived request engine identity changed')
        self.check_consumers(journal, name)
        self.lab.write_private(self.lab.DIRECTORY / 'longhorn-install-business.json', resumed)
        self.path.unlink()
        print('LONGHORN_ARCHIVED_REQUEST_TRANSFERRED_TO_BUSINESS_CLEANUP', flush=True)

    def cleanup(self):
        if not self.path.exists() and not self.path.is_symlink():
            return
        if self.path.is_symlink():
            raise ValueError('symlink request ownership journal')
        row = json.loads(self.path.read_text()); state = self.lab.owner()
        record = self.lab.DIRECTORY / 'longhorn-install-business.json'
        if record.is_symlink() or not record.is_file():
            raise ValueError('request ownership cleanup requires its original business journal')
        journal = json.loads(record.read_text())
        # The rejected false-declaration fixture has exactly one archived
        # recovery boundary; never reinterpret it as a new true declaration.
        if row.get('desired', {}).get('spec', {}).get('createSnapshot') is False:
            self.recover_archived_creation(row, journal, self.current(row))
            return
        _, desired = self.desired(journal, row['scenario'])
        if (any(row.get(key) != state[key] for key in ('run_id', 'cluster_uid', 'namespace_uid')) or
                row.get('desired') != desired or row.get('volume_uid') != journal.get('volume_uid') or
                not row.get('created')):
            raise ValueError('request canary lacks original ownership; retain intent')
        self.source(journal)
        current = self.current(row)
        if current and self.binding(current) != row['created']:
            raise ValueError('request canary was replaced or changed')
        if (row['created'].get('spec', {}).get('createSnapshot') is not True or
                row['created']['spec'].get('volume') != journal['volume']):
            raise ValueError('request canary is not the original snapshot creation declaration')
        name = desired['metadata']['name']
        self.check_consumers(journal, name)
        # A deleted snapshot may remain the parent of volume-head. Transfer
        # the captured UID; normal business cleanup deletes native snapshots
        # and then their source, observing removal without forcing finalizers.
        native = {'uid': row['created']['uid']}
        snapshots = journal.setdefault('native_snapshots', {})
        if name in snapshots and snapshots[name] != native:
            raise ValueError('request snapshot already has another cleanup binding')
        snapshots[name] = native
        self.lab.write_private(record, journal)
        self.path.unlink()
        print('LONGHORN_OWNERSHIP_REQUEST_SNAPSHOT_BOUND_FOR_CLEANUP scenario=' + row['scenario'], flush=True)

    def ready(self, row, journal):
        current = self.current(row)
        if not current or self.binding(current) != row['created']:
            raise ValueError('request canary disappeared or changed before readiness')
        if current['metadata'].get('deletionTimestamp'):
            raise ValueError('request canary is being deleted')
        status = current.get('status', {})
        if status.get('error'):
            raise ValueError('request canary snapshot failed: ' + status['error'])
        name = row['desired']['metadata']['name']
        return (current if status.get('readyToUse') and status.get('creationTime') and
                not status.get('markRemoved') and
                name in self.engine_snapshots(journal, require_running=True)['snapshots'] else None)

    def verify(self, journal, relay_pid):
        if type(relay_pid) is not int or relay_pid <= 0:
            raise ValueError('request qualification requires the live runner relay')
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('request ownership qualification requires no pending canary')
        self.source(journal)
        original = (self.lab.DIRECTORY / 'config.yml').read_bytes()
        expected_engine = self.engine_snapshots(journal, require_running=True)
        shared = self.lab.shared_snapshot_identity(); classes = self.lab.class_identity()
        for scenario in ('foreign', 'immutable'):
            request, desired = self.desired(journal, scenario)
            row = {key: self.lab.owner()[key] for key in ('run_id', 'cluster_uid', 'namespace_uid')}
            row.update(scenario=scenario, desired=desired, volume_uid=journal['volume_uid'])
            if self.current(row):
                raise ValueError('request ownership canary already exists')
            self.lab.write_private(self.path, row)
            with self.lab.scoped_cleanup(self.cleanup):
                obj = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json',
                                                stdin=json.dumps(desired)).stdout)
                if (obj.get('kind') != 'Snapshot' or not obj.get('metadata', {}).get('uid') or
                        obj['metadata'].get('name') != desired['metadata']['name'] or
                        obj['metadata'].get('namespace') != 'longhorn-system'):
                    raise ValueError('request canary creation returned another object')
                row['created'] = self.binding(obj); self.lab.write_private(self.path, row)
                if (obj.get('spec', {}).get('createSnapshot') is not True or
                        obj.get('spec', {}).get('volume') != journal['volume']):
                    raise ValueError('request creation did not preserve snapshot creation intent')
                self.lab.wait('ownership-request-ready-' + scenario, 180, lambda: self.ready(row, journal))
                expected_engine['snapshots'] = sorted(expected_engine['snapshots'] + [request['name']])
                if self.engine_snapshots(journal, require_running=True) != expected_engine:
                    raise ValueError('request canary changed unrelated engine snapshots')
                refusal = ('foreign resource: ' + request['name'] if scenario == 'foreign' else
                           'immutable Longhorn request differs; use a new name')
                self.lab.run_requests(relay_pid, [request], expected_refusal=refusal)
                if not self.ready(row, journal):
                    raise ValueError('product refusal changed original snapshot readiness')
                print('LONGHORN_OWNERSHIP_REQUEST_PRODUCT_REFUSAL_VERIFIED scenario=' + scenario, flush=True)
            journal.update(json.loads((self.lab.DIRECTORY / 'longhorn-install-business.json').read_text()))
            if ((self.lab.DIRECTORY / 'config.yml').read_bytes() != original or
                    self.engine_snapshots(journal) != expected_engine or self.lab.shared_snapshot_identity() != shared or
                    self.lab.class_identity() != classes):
                raise ValueError('request refusal changed original configuration or source engine snapshots')
        self.source(journal)
        print('LONGHORN_OWNERSHIP_REQUEST_REFUSAL_VERIFIED', flush=True)


def cleanup(lab):
    """Recover only exact journals after the request input is restored."""
    for factory in (CSIProbe, PreinstallProbe, InstalledClassProbe, InstalledControllerProbe, InstalledRequestProbe):
        helper = factory(lab)
        if helper.path.exists() or helper.path.is_symlink():
            helper.cleanup()


def interruption_canary(lab):
    """Hold only an independent CRD; never start a product/controller process."""
    import os
    import time
    parent = os.getppid()
    if parent == 1:
        raise ValueError('interruption canary lost its owning parent')
    helper = PreinstallProbe(lab)
    row = {key: lab.owner()[key] for key in ('run_id', 'cluster_uid')}
    row.update(scenario='crd', desired=helper.desired('crd'))
    if (helper.path.exists() or helper.path.is_symlink() or helper.current(row) or
            (lab.DIRECTORY/'longhorn-ownership-csi.json').exists() or
            (lab.DIRECTORY/'longhorn-ownership-csi.json').is_symlink()):
        raise ValueError('interruption canary requires no pending ownership fixture')
    lab.write_private(helper.path, row)
    with lab.scoped_cleanup(helper.cleanup):
        obj = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(row['desired'])).stdout)
        if (obj.get('kind') != 'CustomResourceDefinition' or not obj.get('metadata', {}).get('uid') or
                obj['metadata'].get('name') != row['desired']['metadata']['name']):
            raise ValueError('interruption canary returned another object')
        row['created'] = helper.binding(obj); lab.write_private(helper.path, row)
        lab.wait('ownership-interruption-crd-established', 60, lambda: any(
            c.get('type') == 'Established' and c.get('status') == 'True'
            for c in helper.current(row).get('status', {}).get('conditions', [])))
        print('LONGHORN_OWNERSHIP_INTERRUPTION_CANARY_READY', flush=True)
        while True:
            if os.getppid() != parent:
                raise SystemExit(143)
            time.sleep(1)


def qualify_interruption(lab):
    """Prove Python TERM unwind and exact-journal recovery after KILL."""
    import shutil
    import signal
    import subprocess
    import sys
    import tempfile
    from pathlib import Path
    lab.installation_health()
    original = (lab.DIRECTORY/'config.yml').read_bytes()
    helper = PreinstallProbe(lab)
    pending = ('longhorn-install-business.json', 'longhorn-request-input.json',
               'longhorn-ownership-csi.json', 'longhorn-ownership-preinstall.json', 'longhorn-ownership-class.json',
               'longhorn-ownership-controller.json', 'longhorn-ownership-request.json',
               'longhorn-recurring-credentials.json', 'longhorn-recurring-runtime.json',
               'longhorn-encryption-key.json', 'longhorn-security-tls.json',
               'longhorn-network-probes.json', 'longhorn-reclaim-class.json',
               'longhorn-backup-refusal.json')
    if any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in pending):
        raise ValueError('ownership interruption qualification requires no pending operation')
    before = lab.idempotence_snapshot('longhorn-system')
    shared = lab.shared_snapshot_identity()
    directory = Path(tempfile.mkdtemp(prefix='kubeauto-longhorn-ownership-interruption-', dir='/var/tmp'))
    success = False
    try:
        for label, number, expected in (('term', signal.SIGTERM, 143), ('kill', signal.SIGKILL, -9)):
            log = directory/(label+'.log')
            with log.open('w') as output:
                process = subprocess.Popen([sys.executable, str(lab.BASE/'tests/helpers/longhorn-lab.py'),
                    'ownership-interruption-canary'], stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT, pass_fds=(7, 8, 9))
                try:
                    def ready():
                        if process.poll() is not None:
                            raise ValueError('ownership interruption child exited before its ready boundary')
                        return 'LONGHORN_OWNERSHIP_INTERRUPTION_CANARY_READY' in log.read_text().splitlines()
                    lab.wait('ownership-interruption-child-ready', 90, ready)
                    row = helper.receipt()
                    if not row.get('created') or helper.binding(helper.current(row)) != row['created']:
                        raise ValueError('ownership interruption child lacks its original canary identity')
                    # This PID is an unreaped direct child, which only creates
                    # the declared CRD and waits. No workload/product is killed.
                    process.send_signal(number)
                    lab.wait('ownership-interruption-child-exited', 180, lambda: process.poll() is not None)
                    if process.returncode != expected:
                        raise ValueError('ownership interruption child returned an unexpected exit state')
                    if label == 'kill':
                        if (helper.receipt() != row or helper.binding(helper.current(row)) != row['created']):
                            raise ValueError('killed canary lost its exact recovery journal')
                        # Exercise the fixed outer recovery path, including
                        # config restoration ordering; not a direct deletion.
                        lab.cleanup_business()
                    if helper.path.exists() or helper.path.is_symlink() or helper.current(row):
                        raise ValueError('ownership interruption left its canary or journal')
                    if (lab.DIRECTORY/'config.yml').read_bytes() != original:
                        raise ValueError('ownership interruption changed the product input')
                    if lab.idempotence_snapshot('longhorn-system') != before or lab.shared_snapshot_identity() != shared:
                        raise ValueError('ownership interruption changed original storage/shared identities')
                    print('LONGHORN_OWNERSHIP_INTERRUPTION_OBSERVED signal=' + label +
                          ' rc=' + str(expected) + ' uid=' + row['created']['uid'], flush=True)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try: process.wait(timeout=120)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait(timeout=10)
        lab.installation_health()
        print('LONGHORN_OWNERSHIP_INTERRUPTION_BUSINESS_PASS', flush=True)
        success = True
    finally:
        if success:
            shutil.rmtree(directory)
        else:
            print('LONGHORN_PRIVATE_DIAGNOSTIC directory=' + str(directory), flush=True)
