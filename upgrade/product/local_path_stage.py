"""Internal Local Path image/helper edge; never replay the storage install role.

The outer kubecli path owns immutable observations, artifact admission, lease,
provisioning quiescence, recovery and journal. Business callbacks must check the
existing ledger and a scoped create/write/read/delete path through this driver.
This stage neither deletes customer volumes nor claims parent acceptance.
"""
from copy import deepcopy
import json
from pathlib import PurePosixPath
import time

import yaml

from upgrade.product.calico_stage import CalicoRefused, content
from upgrade.product.dns_stage import DNSStage, DNSRefused, healthy


WORKLOAD = 'deployment/local-path-provisioner'
CONFIG = 'configmap/local-path-config'
PREFIX = 'registry.talkschool.cn:5000/brinnatt/'
SOURCE = PREFIX + 'local-path-provisioner:v0.0.31'
TARGET = PREFIX + 'local-path-provisioner:v0.0.37'
HELPER = PREFIX + 'busybox:1.37'
OBJECTS = {WORKLOAD, CONFIG, 'serviceaccount/local-path-provisioner-service-account',
           'role/local-path-provisioner-role', 'clusterrole/local-path-provisioner-role',
           'rolebinding/local-path-provisioner-bind', 'clusterrolebinding/local-path-provisioner-bind'}


class LocalPathRefused(ValueError):
    """An admitted storage, configuration or data boundary changed."""


class LocalPathCommandFailed(LocalPathRefused):
    def __init__(self, rc):
        self.rc = rc
        super().__init__(f'Local Path command failed (rc={rc}); retain private diagnostics')


def object_content(obj):
    try:
        return content(obj)
    except CalicoRefused:
        raise LocalPathRefused('Local Path object is unidentified or deleting') from None


def helper_template(text, image):
    """Only the delivered safe helper shape is qualified by this edge."""
    try:
        helper = yaml.safe_load(text)
    except yaml.YAMLError:
        raise LocalPathRefused('helper template is not valid YAML') from None
    expected = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'helper-pod'},
        'spec': {'priorityClassName': 'system-node-critical', 'tolerations': [{
            'key': 'node.kubernetes.io/disk-pressure', 'operator': 'Exists', 'effect': 'NoSchedule'}],
            'containers': [{'name': 'helper-pod', 'image': image, 'imagePullPolicy': 'IfNotPresent'}]}}
    if helper != expected:
        raise LocalPathRefused('custom/unsafe helper requires its own applicability review')
    return helper


def validate_config(data):
    if not isinstance(data, dict) or set(data) != {'config.json', 'setup', 'teardown', 'helperPod.yaml'}:
        raise LocalPathRefused('complete delivered ConfigMap data is required')
    scripts = {'setup': '#!/bin/sh\nset -eu\nmkdir -m 0777 -p "$VOL_DIR"',
               'teardown': '#!/bin/sh\nset -eu\nrm -rf "$VOL_DIR"'}
    if any(data[key].strip() != script for key, script in scripts.items()):
        raise LocalPathRefused('custom directory scripts require separate review')
    try:
        config = json.loads(data['config.json'])
    except (TypeError, ValueError):
        raise LocalPathRefused('Local Path config is not valid JSON') from None
    if (not isinstance(config, dict) or set(config) != {'nodePathMap'} or
            not isinstance(config['nodePathMap'], list) or not config['nodePathMap']):
        raise LocalPathRefused('only the delivered nodePathMap configuration is qualified')
    seen = set()
    for entry in config['nodePathMap']:
        if (not isinstance(entry, dict) or set(entry) != {'node', 'paths'} or
                not isinstance(entry['node'], str) or not entry['node'] or entry['node'] in seen or
                not isinstance(entry['paths'], list)):
            raise LocalPathRefused('nodePathMap nodes must be named and unique')
        seen.add(entry['node'])
        paths = entry['paths']
        if not all(isinstance(p, str) and p.startswith('/') and
                   str(PurePosixPath(p)) == p and p != '/' and '..' not in PurePosixPath(p).parts
                   for p in paths) or len(set(paths)) != len(paths):
            raise LocalPathRefused('nodePathMap requires unique normalized non-root absolute paths')


class LocalPathStage:
    def __init__(self, kubectl, kubeconfig, source, *, timeout=600, progress=None):
        self.uid = source.get('cluster_identity', {}).get('uid')
        nodes = source.get('nodes', [])
        if (source.get('source') != 'v1.33.6' or source.get('target') != 'v1.34.13' or
                source.get('mutation') != 'none' or not self.uid or
                source.get('cluster_identity', {}).get('namespace') != 'kube-system' or
                not nodes or len({n.get('name') for n in nodes}) != len(nodes) or
                not all(n.get('name') and n.get('uid') for n in nodes) or
                len({n['uid'] for n in nodes}) != len(nodes)):
            raise LocalPathRefused('complete admitted cluster/node source identities required')
        self.transport = DNSStage(kubectl, kubeconfig,
            [b['server'] for b in source.get('api_backends', [])],
            {n['name']: n['uid'] for n in nodes}, timeout=timeout, progress=progress)
        self.progress = self.transport.progress
        self.timeout = timeout

    def command(self, args, *, read=False):
        try:
            return self.transport.read(args) if read else self.transport.command(args)
        except DNSRefused as error:
            if hasattr(error, 'rc'):
                raise LocalPathCommandFailed(error.rc) from None
            raise LocalPathRefused(str(error)) from None

    def barrier(self):
        try:
            self.transport.platform_barrier()
            for backend in self.transport.backends:
                obj = self.transport.read(['get', 'namespace', 'kube-system', '-o', 'json'], backend)
                if (obj.get('metadata', {}).get('uid') != self.uid or
                        obj.get('metadata', {}).get('deletionTimestamp') or
                        obj.get('status', {}).get('phase') != 'Active'):
                    raise LocalPathRefused('Local Path API backend cluster identity/health differs')
        except DNSRefused as error:
            if hasattr(error, 'rc'):
                raise LocalPathCommandFailed(error.rc) from None
            raise LocalPathRefused(str(error)) from None

    def check(self, expected):
        observed = {}
        for ref, wanted in expected.items():
            args = ['get', '--raw='+ref] if ref.startswith('/api/') else ['get', ref, '-o', 'json']
            obj = self.command(args, read=True)
            if object_content(obj) != object_content(wanted):
                raise LocalPathRefused('Local Path object identity/configuration differs: '+ref)
            if obj.get('kind') in {'PersistentVolume', 'PersistentVolumeClaim'} and (
                    obj.get('status', {}).get('phase') != 'Bound'):
                raise LocalPathRefused('admitted Local Path volume/claim is no longer Bound')
            observed[ref] = obj
        return observed

    def patch(self, ref, obj, path, before, after, *, dry_run):
        meta = obj['metadata']
        if not meta.get('resourceVersion'):
            raise LocalPathRefused('guarded patch requires current resourceVersion')
        operations = [{'op': 'test', 'path': '/metadata/'+k, 'value': meta[k]}
                      for k in ['uid', 'resourceVersion']]
        operations += [{'op': 'test', 'path': path, 'value': before},
                       {'op': 'replace', 'path': path, 'value': after}]
        args = ['patch', ref, '--type=json', '-p', json.dumps(operations)]
        return self.command(args + (['--dry-run=server', '-o', 'json'] if dry_run else []), read=dry_run)

    def run(self, reviewed, volumes, verify_data):
        """Preserve admitted objects and bound volumes; helper first, controller next.

        The callback owns business I/O and scratch lifecycle, including cleanup.
        On resume, only source/source, target-helper/source and target/target are
        admitted. There is no rollback, deletion or unsafe-template opt-out.
        """
        if (not isinstance(reviewed, dict) or len(set(reviewed)-OBJECTS) != 1 or
                not OBJECTS <= set(reviewed) or not isinstance(volumes, dict) or not volumes or
                not callable(verify_data)):
            raise LocalPathRefused('complete reviewed objects, bound PV/PVC and real data gate required')
        storage_ref = next(iter(set(reviewed)-OBJECTS))
        storage = reviewed[storage_ref]
        if (storage_ref != 'storageclass/'+storage.get('metadata', {}).get('name', '') or
                storage.get('kind') != 'StorageClass' or storage.get('apiVersion') != 'storage.k8s.io/v1' or
                storage.get('provisioner') != 'rancher.io/local-path' or
                storage.get('volumeBindingMode') != 'WaitForFirstConsumer' or
                storage.get('reclaimPolicy') != 'Delete' or storage.get('parameters') or
                'allowUnsafePathPattern' in storage.get('metadata', {}).get('annotations', {})):
            raise LocalPathRefused('custom StorageClass/path patterns require separate applicability review')
        classes = storage['metadata']['name']
        pvcs = {v.get('metadata', {}).get('uid'): v for v in volumes.values()
                if v.get('kind') == 'PersistentVolumeClaim'}
        pvs = [v for v in volumes.values() if v.get('kind') == 'PersistentVolume']
        if not pvs or len(pvs) != len(pvcs) or len(volumes) != 2*len(pvs):
            raise LocalPathRefused('complete one-to-one admitted bound PV/PVC pairs required')
        for pv in pvs:
            claim = pv.get('spec', {}).get('claimRef', {})
            pvc = pvcs.get(claim.get('uid'), {})
            meta = pvc.get('metadata', {})
            if (pv.get('spec', {}).get('storageClassName') != classes or
                    pvc.get('spec', {}).get('storageClassName') != classes or
                    pvc.get('spec', {}).get('volumeName') != pv.get('metadata', {}).get('name') or
                    claim.get('namespace') != meta.get('namespace') or claim.get('name') != meta.get('name')):
                raise LocalPathRefused('admitted PV/PVC binding differs')
        for ref, obj in volumes.items():
            meta = obj.get('metadata', {})
            path = ('/api/v1/persistentvolumes/'+meta.get('name', '') if obj.get('kind') == 'PersistentVolume'
                    else '/api/v1/namespaces/'+meta.get('namespace', '')+
                         '/persistentvolumeclaims/'+meta.get('name', ''))
            if ref != path:
                raise LocalPathRefused('volume API path differs from its admitted identity')
        source = deepcopy(reviewed)
        deployment = source[WORKLOAD]
        containers = deployment.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
        if (deployment.get('apiVersion') != 'apps/v1' or deployment.get('kind') != 'Deployment' or
                deployment.get('metadata', {}).get('namespace') != 'kube-system' or
                len(containers) != 1 or containers[0].get('name') != 'local-path-provisioner' or
                containers[0].get('image') != SOURCE or
                containers[0].get('command') != ['local-path-provisioner', '--debug', 'start', '--config', '/etc/config/config.json'] or
                {'name': 'CONFIG_MOUNT_PATH', 'value': '/etc/config/'} not in containers[0].get('env', []) or
                any(e.get('name') == 'ALLOW_UNSAFE_HELPER_POD_TEMPLATE' for e in containers[0].get('env', []))):
            raise LocalPathRefused('source controller image/config/unsafe-template boundary differs')
        data = source[CONFIG].get('data')
        validate_config(data)
        helper = helper_template(data['helperPod.yaml'], SOURCE)
        helper['spec']['containers'][0]['image'] = HELPER
        helper_text = yaml.safe_dump(helper, sort_keys=False)
        desired = deepcopy(source)
        desired[CONFIG]['data']['helperPod.yaml'] = helper_text
        desired[WORKLOAD]['spec']['template']['spec']['containers'][0]['image'] = TARGET
        self.barrier()
        expected = deepcopy(source)
        current = {ref: self.command(['get', ref, '-o', 'json'], read=True) for ref in [CONFIG, WORKLOAD]}
        current_helper = current[CONFIG].get('data', {}).get('helperPod.yaml')
        image = current[WORKLOAD].get('spec', {}).get('template', {}).get('spec', {}).get('containers', [{}])[0].get('image')
        if (current_helper not in [data['helperPod.yaml'], helper_text] or image not in [SOURCE, TARGET] or
                (image == TARGET and current_helper != helper_text)):
            raise LocalPathRefused('Local Path state is outside the ordered image/helper edge')
        expected[CONFIG]['data']['helperPod.yaml'] = current_helper
        expected[WORKLOAD]['spec']['template']['spec']['containers'][0]['image'] = image
        observed = self.check({**expected, **volumes})
        if image == SOURCE and not healthy(observed[WORKLOAD]):
            raise LocalPathRefused('source Local Path controller is unhealthy')
        verify_data('existing')
        changes = [(CONFIG, '/data/helperPod.yaml', current_helper, helper_text),
                   (WORKLOAD, '/spec/template/spec/containers/0/image', image, TARGET)]
        # Qualify every required write before performing the first mutation.
        for ref, path, before, after in changes:
            if before != after and object_content(self.patch(ref, observed[ref], path, before, after,
                    dry_run=True)) != object_content(desired[ref]):
                raise LocalPathRefused('Local Path server dry-run changed more than the reviewed delta')
        for ref, path, before, after in changes:
            if before == after:
                continue
            self.barrier()
            observed = self.check({**expected, **volumes})
            verify_data('existing')
            self.patch(ref, observed[ref], path, before, after, dry_run=False)
            expected[ref] = desired[ref]
        deadline = time.monotonic()+self.timeout
        while True:
            self.barrier()
            observed = self.check({**desired, **volumes})
            if healthy(observed[WORKLOAD]):
                break
            if time.monotonic() >= deadline:
                raise LocalPathRefused('Local Path target rollout exceeded fixed timeout')
            self.progress('Local Path: waiting for target observed generation and all replicas')
            time.sleep(5)
        # Ready alone cannot prove that a reloaded helper/config was accepted.
        verify_data('target-provision-and-cleanup')
        verify_data('existing')
        self.check({**desired, **volumes})
        self.progress('UPGRADE_LOCAL_PATH_IMAGE_AND_DATA_VERIFIED')
        return {'image': TARGET, 'helper': HELPER,
                'objects': {ref: obj['metadata']['uid'] for ref, obj in observed.items()},
                'parent_acceptance': False}
