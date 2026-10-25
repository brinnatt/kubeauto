#!/usr/bin/env python3
"""Longhorn installation and immutable operations; native controllers own I/O.

Runs on the Ansible control interpreter (project-pinned PyYAML is required).
No command logs Secrets, changes replica files, or force-removes finalizers.
"""
import argparse
import base64
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time

import yaml

FILES = Path(__file__).resolve().parent
NAMESPACE = 'longhorn-system'
LABELS = {'app.kubernetes.io/managed-by': 'kubeauto', 'kubeauto.io/component': 'longhorn'}
OWNER = {'kubeauto.io/component': 'longhorn'}
SNAPSHOT_OWNER = {'kubeauto.io/component': 'longhorn-snapshot'}
SNAPSHOT_CRDS = ('volumesnapshots', 'volumesnapshotcontents', 'volumesnapshotclasses')
CHART_HASHES = {
    '1.12.1': 'c8cf4b35a9d872cd5f7e44fd26d8e6ac7c2abaee42f4e2f2a0b0ebbc6e3a6116',
    '1.12.0': '869bb20701b154473606f1e8967b27f34f2448a2dfe6eb8970f1cae6957384f5',
}


def name(value):
    if not isinstance(value, str) or len(value) > 63 or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', value):
        raise ValueError('invalid Longhorn resource name')
    return value


def flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ('yes', 'no', 'true', 'false'):
        return value.lower() in ('yes', 'true')
    raise ValueError('Longhorn boolean requires yes/no or true/false')


def integer(value, low, high):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)) or not low <= int(value) <= high:
        raise ValueError('Longhorn integer outside supported range')
    return int(value)


def owned(obj, labels=OWNER):
    return all(obj.get('metadata', {}).get('labels', {}).get(k) == v for k, v in labels.items())


def require_owned(obj, labels=OWNER):
    if obj is not None and not owned(obj, labels):
        raise ValueError('foreign resource: ' + obj.get('metadata', {}).get('name', 'unknown'))


def engines(c):
    # Preserve existing V1-only configurations that predate explicit selection.
    return c.get('longhorn_data_engines', ['v1'])


def validate_engines(c, inventory, clients, replicas):
    selected = engines(c)
    if (not isinstance(selected, list) or not selected or
            any(x not in ('v1', 'v2') for x in selected) or len(selected) != len(set(selected))):
        raise ValueError('Longhorn data engines require a unique nonempty v1/v2 list')
    if len(selected) == 2 and not flag(c.get('longhorn_allow_engine_coexistence', False)):
        raise ValueError('Longhorn dual-engine operation requires explicit resource-budget approval')
    v2_nodes = c.get('longhorn_v2_nodes', [])
    if (not isinstance(v2_nodes, list) or not all(isinstance(n, str) for n in v2_nodes)
            or len(v2_nodes) != len(set(v2_nodes)) or not set(v2_nodes) <= set(clients)):
        raise ValueError('Longhorn V2 nodes must be unique client inventory addresses')
    block = c.get('longhorn_block_disks', {})
    if not isinstance(block, dict) or not set(block) <= set(v2_nodes):
        raise ValueError('Longhorn block disks require V2-enabled inventory addresses')
    if 'v1' not in selected and c['longhorn_storage_nodes']:
        raise ValueError('disabled V1 engine cannot own filesystem storage nodes')
    if 'v2' not in selected and (v2_nodes or block):
        raise ValueError('disabled V2 engine cannot own nodes or block disks')
    for engine, nodes in (('v1', c['longhorn_storage_nodes']), ('v2', block)):
        if engine in selected and len(nodes) < replicas:
            raise ValueError('Longhorn engine requires independent storage nodes for its replica count: ' + engine)
    mask = c.get('longhorn_v2_cpu_mask', '0x3')
    if not isinstance(mask, str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', mask) or not int(mask, 16):
        raise ValueError('Longhorn V2 CPU mask requires a nonzero hexadecimal bitmask')
    cores = integer(c.get('longhorn_v2_cpu_cores', 0), 0, 256)
    if cores and ('v2' not in selected or c['longhorn_version'] != '1.12.1'):
        raise ValueError('exclusive CPU allocation requires enabled V2 and Longhorn 1.12.1')
    integer(c.get('longhorn_v2_cpu_reservation_percentage', 25), 1, 40)
    integer(c.get('longhorn_v2_memory_mib', 2048), 2048, 1048576)
    flag(c.get('longhorn_v2_hugepages', True)); flag(c.get('longhorn_v2_interrupt_mode', False))
    # The pinned controller limits exclusive IM ordinary memory to 128Mi;
    # anonymous SPDK preallocates at least 2048Mi in the same cgroup.
    if cores and not flag(c.get('longhorn_v2_hugepages', True)):
        raise ValueError('V2 anonymous memory with exclusive CPU requires an instance-manager memory limit covering SPDK allocation')
    isolation = flag(c.get('longhorn_v2_cpu_isolation_enabled', False))
    if isolation and ('v2' not in selected or c['longhorn_version'] != '1.12.1'):
        raise ValueError('host CPU isolation requires enabled V2 and Longhorn 1.12.1')
    large = integer(c.get('longhorn_v2_iobuf_large_pool_size', 1024), 1024, 2**63 - 1)
    small = integer(c.get('longhorn_v2_iobuf_small_pool_size', 8192), 8192, 2**63 - 1)
    if (large, small) != (1024, 8192) and ('v2' not in selected or c['longhorn_version'] != '1.12.1'):
        raise ValueError('iobuf tuning requires enabled V2 and Longhorn 1.12.1')
    # Fixed upstream buffer sizes; SPDK also needs memory outside these pools.
    if large * 132 + small * 8 >= int(c.get('longhorn_v2_memory_mib', 2048)) * 1024:
        raise ValueError('iobuf pools exhaust the configured SPDK memory budget; increase memory first')
    bandwidth = c.get('longhorn_v2_rebuild_bandwidth_limit')
    if bandwidth is not None:
        integer(bandwidth, 0, 2**63 - 1)
        if 'v2' not in selected:
            raise ValueError('replica rebuild bandwidth limit requires enabled V2')
    for address, disks in block.items():
        if not isinstance(disks, list) or not disks or len(disks) > 32:
            raise ValueError('Longhorn block disks require a bounded nonempty list per node')
        names, paths = set(), set()
        for disk in disks:
            if not isinstance(disk, dict) or set(disk) - {'allow_scheduling', 'eviction_requested'} != {'name', 'path', 'disk_driver', 'identity', 'storage_reserved'}:
                raise ValueError('invalid Longhorn block disk fields')
            if ('allow_scheduling' in disk) != ('eviction_requested' in disk):
                raise ValueError('Longhorn block disk maintenance flags must be paired')
            if 'allow_scheduling' in disk:
                scheduling = flag(disk['allow_scheduling'])
                eviction = flag(disk['eviction_requested'])
                if scheduling and eviction:
                    raise ValueError('Longhorn disk eviction requires disabled scheduling')
            name(disk['name'])
            if disk['name'] == 'kubeauto-data':
                raise ValueError('reserved Longhorn filesystem disk name')
            if disk['name'] in names or disk['path'] in paths:
                raise ValueError('duplicate Longhorn block disk name or path')
            names.add(disk['name']); paths.add(disk['path'])
            # Native PCI passthrough needs a different host identity/driver
            # lifecycle. Never silently auto-select it from an AIO declaration.
            if disk['disk_driver'] != 'aio' or not isinstance(disk['path'], str) or not re.fullmatch(r'/dev/disk/by-id/[A-Za-z0-9_.:+-]+', disk['path']):
                raise ValueError('Longhorn managed block disks currently require explicit aio and stable by-id paths; native PCI qualification pending')
            if not isinstance(disk['identity'], str) or not re.fullmatch(r'[A-Za-z0-9_.:+-]{1,256}', disk['identity']):
                raise ValueError('Longhorn block disk requires an explicit hardware WWN or serial identity')
            integer(disk['storage_reserved'], 0, 2**63 - 1)


def quantity(value):
    """Parse the CPU/memory quantity forms used by Kubernetes node capacity."""
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)', str(value))
    units = {'': 1, 'm': Decimal('0.001'), 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3,
             'Ti': 1024**4, 'Pi': 1024**5, 'K': 1000, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4}
    if not match or match[2] not in units:
        raise ValueError('unsupported Kubernetes capacity quantity')
    return Decimal(match[1]) * units[match[2]]


def check_exclusive_cpu_budget(c, node, override=0):
    """Upstream rounds guaranteed millicores, then grants whole exclusive CPUs."""
    cpu = quantity(node.get('status', {}).get('allocatable', {}).get('cpu', 0))
    request = Decimal(override) if override else (
        cpu * 1000 * Decimal(c.get('longhorn_v2_cpu_reservation_percentage', 25)) / 100
    ).to_integral_value(rounding=ROUND_HALF_UP)
    granted = max(Decimal(int(c.get('longhorn_v2_cpu_cores', 0))),
                  (request / 1000).to_integral_value(rounding=ROUND_CEILING))
    if granted > cpu:
        raise ValueError('V2 exclusive CPU request exceeds node allocatable CPU')


def check_v2_resources(c, node):
    available = node.get('status', {}).get('allocatable', {})
    cpu = quantity(available.get('cpu', 0))
    mask = int(c.get('longhorn_v2_cpu_mask', '0x3'), 16)
    exclusive = int(c.get('longhorn_v2_cpu_cores', 0))
    cores = exclusive or mask.bit_count()
    percentage = Decimal(c.get('longhorn_v2_cpu_reservation_percentage', 25))
    # Node allocatable CPU is a quantity, not the largest online CPU ID.
    # block-check.py independently verifies mask membership against host sysfs.
    if cores > cpu or (not exclusive and cpu * percentage / 100 < cores):
        raise ValueError('V2 allocatable CPU and guaranteed reservation must cover SPDK reactor cores')
    if exclusive:
        check_exclusive_cpu_budget(c, node)
    memory = int(c.get('longhorn_v2_memory_mib', 2048)) * 1024**2
    # This is admission headroom for the IM, not a complete workload sizing model.
    if quantity(available.get('memory', 0)) < memory + 1024**3:
        raise ValueError('V2 allocatable memory lacks SPDK and instance-manager headroom')
    if flag(c.get('longhorn_v2_hugepages', True)) and quantity(available.get('hugepages-2Mi', 0)) < memory:
        raise ValueError('V2 kubelet hugepages-2Mi capacity is below SPDK memory size')


def validate(c):
    enabled = flag(c['longhorn_install'])
    if not enabled:
        return
    if c['longhorn_version'] not in CHART_HASHES or c['longhorn_namespace'] != NAMESPACE:
        raise ValueError('Longhorn requires a pinned version and longhorn-system namespace')
    if c['longhorn_action'] not in ('present', 'uninstall', 'retire-block-disks'):
        raise ValueError('Longhorn action must be present, uninstall or retire-block-disks')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*(?:/[A-Za-z0-9._-]+)+', c['longhorn_image_prefix']):
        raise ValueError('Longhorn image prefix requires a registry/repository without credentials')
    for key in ('grpc_tls_required', 'monitoring_enabled'):
        flag(c['longhorn_' + key])
    for key in ('ready_timeout', 'operation_timeout'):
        integer(c['longhorn_' + key], 60, 86400)
    nodes = c['longhorn_storage_nodes']
    if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes) or len(nodes) != len(set(nodes)):
        raise ValueError('Longhorn storage nodes require a unique inventory-address list')
    replicas = integer(c['longhorn_default_replica_count'], 1, 5)
    inventory = c.get('longhorn_inventory_nodes', [])
    if not set(nodes) <= set(inventory):
        raise ValueError('Longhorn storage address is outside the Kubernetes inventory')
    clients = c.get('longhorn_client_nodes', [])
    if (not isinstance(clients, list) or not all(isinstance(n, str) for n in clients)
            or len(clients) != len(set(clients)) or not set(nodes) <= set(clients) <= set(inventory)):
        raise ValueError('Longhorn storage nodes must belong to the kube_node client inventory')
    validate_engines(c, inventory, clients, replicas)
    path = c['longhorn_data_path']
    if not isinstance(path, str) or not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+', path) or path.startswith(('/var/lib/kubelet/', '/var/lib/containerd/', '/var/lib/docker/')):
        raise ValueError('Longhorn data path requires a dedicated mount root, not a system directory')
    integer(c['longhorn_min_disk_gib'], 20, 1048576)
    if c['longhorn_snapshot_controller'] not in ('managed', 'external', 'disabled'):
        raise ValueError('snapshot-controller must be managed, external or disabled')
    classes = c['longhorn_storage_classes']
    if not isinstance(classes, list) or not classes or len(classes) > 20:
        raise ValueError('Longhorn requires an explicit StorageClass list')
    seen, defaults = set(), 0
    for sc in classes:
        if not isinstance(sc, dict) or set(sc) - {'data_engine', 'clone_mode'} != {'name', 'replicas', 'fs_type', 'reclaim_policy', 'default', 'encrypted', 'encryption_secret', 'recurring_jobs'}:
            raise ValueError('invalid Longhorn StorageClass fields')
        name(sc['name'])
        engine = sc.get('data_engine', 'v1')
        if engine not in engines(c):
            raise ValueError('StorageClass data engine is not enabled')
        if sc.get('clone_mode', 'full-copy') not in ('full-copy', 'linked-clone') or (
                sc.get('clone_mode') == 'linked-clone' and engine != 'v2'):
            raise ValueError('linked-clone requires a V2 StorageClass')
        engine_nodes = nodes if engine == 'v1' else c['longhorn_block_disks']
        if sc['name'] in seen or integer(sc['replicas'], 1, 5) > len(engine_nodes):
            raise ValueError('duplicate StorageClass or insufficient independent storage nodes')
        seen.add(sc['name'])
        if sc['fs_type'] not in ('ext4', 'xfs') or sc['reclaim_policy'] not in ('Retain', 'Delete'):
            raise ValueError('unsupported Longhorn filesystem or reclaim policy')
        defaults += flag(sc['default'])
        if flag(sc['encrypted']):
            name(sc['encryption_secret'])
        elif sc['encryption_secret']:
            raise ValueError('unencrypted StorageClass cannot reference an encryption key')
        if not isinstance(sc['recurring_jobs'], list):
            raise ValueError('StorageClass recurring jobs must be a list')
        for job in sc['recurring_jobs']:
            if not isinstance(job, dict) or set(job) != {'name', 'isGroup'}:
                raise ValueError('recurring-job selector requires name/isGroup')
            name(job['name']); flag(job['isGroup'])
    if defaults > 1:
        raise ValueError('only one Longhorn class can be default')
    if c['longhorn_backup_target']:
        target = c['longhorn_backup_target']
        if not isinstance(target, str) or not re.fullmatch(r'(s3://[a-z0-9][a-z0-9.-]*@[a-z0-9-]+/[A-Za-z0-9._/-]*|nfs://[A-Za-z0-9.-]+:/[A-Za-z0-9._/-]+)', target):
            raise ValueError('backup target requires an explicit credential-free S3 or NFS URL')
        if target.startswith('s3://'):
            name(c['longhorn_backup_secret'])
    if c['longhorn_backup_secret']:
        name(c['longhorn_backup_secret'])
    jobs = c['longhorn_recurring_jobs']
    if not isinstance(jobs, list) or len(jobs) > 20:
        raise ValueError('Longhorn recurring jobs require an explicit bounded list')
    seen = set()
    for job in jobs:
        required = {'name', 'task', 'cron', 'retain', 'concurrency', 'groups'}
        if not isinstance(job, dict) or not required <= set(job) or set(job) - required - {'parameters'}:
            raise ValueError('recurring job requires name/task/cron/retain/concurrency/groups')
        name(job['name'])
        if len(job['name']) > 40:
            raise ValueError('Longhorn recurring job names are limited to 40 characters')
        if job['name'] in seen or job['task'] not in ('snapshot', 'backup', 'backup-force-create'):
            raise ValueError('duplicate or unsupported recurring job; destructive retention tasks are native operations')
        seen.add(job['name'])
        if not isinstance(job['cron'], str) or len(job['cron'].split()) != 5 or not re.fullmatch(r'[0-9*,/ -]+', job['cron']):
            raise ValueError('recurring job requires five numeric cron fields')
        integer(job['retain'], 1, 365); integer(job['concurrency'], 1, 10)
        if not isinstance(job['groups'], list):
            raise ValueError('recurring job groups must be a list')
        for group in job['groups']:
            name(group)
        if job['task'].startswith('backup') and not c['longhorn_backup_target']:
            raise ValueError('recurring backup requires an explicit backup target')
        parameters = job.get('parameters', {})
        if (not isinstance(parameters, dict) or set(parameters) - {'full-backup-interval'} or
                (parameters and not job['task'].startswith('backup'))):
            raise ValueError('only backup tasks accept the full-backup-interval parameter')
        for value in parameters.values():
            if not isinstance(value, str):
                raise ValueError('recurring backup parameters must be strings')
            integer(value, 0, 2147483647)
    if flag(c['longhorn_monitoring_enabled']):
        if c['longhorn_version'] != '1.12.1':
            raise ValueError('restricted monitoring requires Longhorn 1.12.1 network policies')
        name(c['longhorn_monitoring_namespace']); name(c['longhorn_monitoring_release'])
        selector = c['longhorn_monitoring_selector']
        if not isinstance(selector, dict) or not selector or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in selector.items()):
            raise ValueError('monitoring requires an explicit nonempty Pod label selector')
    requests = c['longhorn_requests']
    if not isinstance(requests, list) or len(requests) > 20:
        raise ValueError('Longhorn requests must be a bounded list')
    seen = set()
    for request in requests:
        obj = request_object(request)
        identity = (obj['kind'], obj['metadata']['name'])
        if identity in seen:
            raise ValueError('duplicate Longhorn request')
        seen.add(identity)
    for key in ('upgrade_id', 'upgrade_system_backup', 'uninstall_id'):
        if c['longhorn_' + key]:
            name(c['longhorn_' + key])
    if c['longhorn_action'] == 'retire-block-disks':
        if c['longhorn_requests']:
            raise ValueError('block retirement cannot combine immutable volume requests')
        block_retirement().validate(c)
    if c['longhorn_action'] == 'uninstall' and not c['longhorn_uninstall_id']:
        raise ValueError('Longhorn uninstall requires an explicit change ID')


def resource(kind, object_name, spec=None, api='longhorn.io/v1beta2', namespace=NAMESPACE):
    obj = {'apiVersion': api, 'kind': kind, 'metadata': {'name': object_name, 'labels': dict(LABELS)}}
    if namespace:
        obj['metadata']['namespace'] = namespace
    if spec is not None:
        obj['spec'] = spec
    return obj


def request_object(r):
    if not isinstance(r, dict):
        raise ValueError('Longhorn request must be a mapping')
    name(r.get('name'))
    fields = {'snapshot': {'volume'}, 'backup': {'snapshot', 'mode'},
              'system-backup': {'volume_backup_policy'}, 'system-restore': {'source'}}
    kind = r.get('kind')
    if kind not in fields or set(r) != fields[kind] | {'kind', 'name'}:
        raise ValueError('invalid Longhorn operation fields')
    if kind == 'snapshot':
        spec = {'volume': name(r['volume']), 'createSnapshot': True, 'labels': {}}
        native = 'Snapshot'
    elif kind == 'backup':
        if r['mode'] not in ('full', 'incremental'):
            raise ValueError('backup mode must be full or incremental')
        spec = {'snapshotName': name(r['snapshot']), 'backupMode': r['mode'], 'labels': {}}
        native = 'Backup'
    elif kind == 'system-backup':
        if r['volume_backup_policy'] not in ('always', 'if-not-present', 'disabled'):
            raise ValueError('invalid system-backup volume policy')
        spec = {'volumeBackupPolicy': r['volume_backup_policy']}
        native = 'SystemBackup'
    else:
        spec = {'systemBackup': name(r['source'])}
        native = 'SystemRestore'
    obj = resource(native, r['name'], spec)
    obj['metadata']['annotations'] = {'kubeauto.io/request-sha256': hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()}
    return obj


def storage_class(sc):
    obj = resource('StorageClass', sc['name'], api='storage.k8s.io/v1', namespace='')
    obj.update(provisioner='driver.longhorn.io', reclaimPolicy=sc['reclaim_policy'],
               allowVolumeExpansion=True, volumeBindingMode='Immediate', parameters={
                   'numberOfReplicas': str(sc['replicas']), 'fsType': sc['fs_type'], 'dataEngine': sc.get('data_engine', 'v1'),
                   'replicaSoftAntiAffinity': 'disabled', 'dataLocality': 'disabled',
                   'encrypted': str(flag(sc['encrypted'])).lower(),
                   'recurringJobSelector': json.dumps(sc['recurring_jobs'], separators=(',', ':'))})
    obj['metadata']['annotations'] = {'storageclass.kubernetes.io/is-default-class': str(flag(sc['default'])).lower()}
    if sc.get('data_engine') == 'v2':
        obj['parameters'].update({'frontend': 'blockdev', 'dataLayout.type': 'replicated',
                                  'cloneMode': sc.get('clone_mode', 'full-copy')})
    if flag(sc['encrypted']):
        for operation in ('provisioner', 'node-stage', 'node-publish', 'node-expand'):
            obj['parameters']['csi.storage.k8s.io/' + operation + '-secret-name'] = sc['encryption_secret']
            obj['parameters']['csi.storage.k8s.io/' + operation + '-secret-namespace'] = NAMESPACE
    return obj


def chart_values(c):
    tags = json.loads((FILES / 'images.json').read_text())[c['longhorn_version']]
    prefix = c['longhorn_image_prefix']
    registry, repository = prefix.split('/', 1)
    lh = {'engine': 'engine', 'manager': 'manager', 'ui': 'ui', 'instanceManager': 'instance-manager',
          'shareManager': 'share-manager', 'backingImageManager': 'backing-image-manager', 'supportBundleKit': 'support-bundle-kit'}
    csi = {'attacher': 'csi-attacher', 'provisioner': 'csi-provisioner', 'nodeDriverRegistrar': 'csi-node-driver-registrar',
           'resizer': 'csi-resizer', 'snapshotter': 'csi-snapshotter', 'livenessProbe': 'livenessprobe'}
    def images(mapping):
        return {key: {'registry': '', 'repository': repository + '/longhorn-' + value,
                      'tag': tags['longhorn-' + value]} for key, value in mapping.items()}
    values = {
        'global': {'imageRegistry': registry},
        'image': {'longhorn': images(lh), 'csi': images(csi)},
        'persistence': {'createStorageClass': False},
        'csi': {'kubeletRootDir': c.get('longhorn_kubelet_root', '/var/lib/kubelet'), 'podAntiAffinityPreset': 'hard'},
        'longhornUI': {'replicas': 2},
        'longhornManager': {'nodeSelector': {'kubeauto.io/longhorn-client': 'true'}, 'tolerations': [
            {'key': key, 'operator': 'Exists', 'effect': 'NoSchedule'}
            for key in ('node-role.kubernetes.io/control-plane', 'node-role.kubernetes.io/master')]},
        'longhornDriver': {'tolerations': [
            {'key': key, 'operator': 'Exists', 'effect': 'NoSchedule'}
            for key in ('node-role.kubernetes.io/control-plane', 'node-role.kubernetes.io/master')]},
        'defaultSettings': {'taintToleration': 'node-role.kubernetes.io/control-plane:NoSchedule;node-role.kubernetes.io/master:NoSchedule',
            'createDefaultDiskLabeledNodes': True, 'defaultDataPath': c['longhorn_data_path'],
            'defaultReplicaCount': {engine: str(c['longhorn_default_replica_count']) for engine in ('v1', 'v2')},
            'replicaSoftAntiAffinity': False, 'replicaAutoBalance': 'disabled',
            'allowVolumeCreationWithDegradedAvailability': False,
            'storageOverProvisioningPercentage': 100, 'storageMinimalAvailablePercentage': 25,
            'storageReservedPercentageForDefaultDisk': 10, 'nodeDrainPolicy': 'block-if-contains-last-replica',
            'autoSalvage': False, 'upgradeChecker': False, 'v1DataEngine': 'v1' in engines(c), 'v2DataEngine': 'v2' in engines(c),
            'freezeFilesystemForSnapshot': {'v1': 'false', 'v2': 'false'}},
        'metrics': {'serviceMonitor': {'enabled': flag(c['longhorn_monitoring_enabled']),
            'additionalLabels': {'release': c['longhorn_monitoring_release']}, 'interval': '30s', 'scrapeTimeout': '10s'}},
        'networkPolicies': {'restrictInternalTraffic': True, 'enabled': False},
        'preUpgradeChecker': {'jobEnabled': True, 'upgradeVersionCheck': True},
    }
    clients = c.get('longhorn_client_nodes', [])
    if 0 < len(clients) < 3:
        # CSI controllers retain hard node anti-affinity. Smaller, explicitly
        # configured clusters cannot schedule the upstream three-copy default.
        values['csi'].update({key + 'ReplicaCount': len(clients)
                              for key in ('attacher', 'provisioner', 'resizer', 'snapshotter')})
    if 'v2' in engines(c):
        values['defaultSettings'].update(
            dataEngineCPUMask={'v2': c.get('longhorn_v2_cpu_mask', '0x3')},
            guaranteedInstanceManagerCPU={'v1': '12', 'v2': str(c.get('longhorn_v2_cpu_reservation_percentage', 25))},
            dataEngineMemorySize={'v2': str(c.get('longhorn_v2_memory_mib', 2048))},
            dataEngineHugepageEnabled={'v2': str(flag(c.get('longhorn_v2_hugepages', True))).lower()},
            dataEngineInterruptModeEnabled={'v2': str(flag(c.get('longhorn_v2_interrupt_mode', False))).lower()})
        if c['longhorn_version'] == '1.12.1':
            if 'longhorn_v2_cpu_isolation_enabled' in c:
                values['defaultSettings']['dataEngineCPUIsolationEnabled'] = {
                    'v2': str(flag(c['longhorn_v2_cpu_isolation_enabled'])).lower()}
            values['defaultSettings'].update(
                dataEngineNumberOfCPUCores={'v2': str(c.get('longhorn_v2_cpu_cores', 0))},
                dataEngineIobufLargePoolSize={'v2': str(c.get('longhorn_v2_iobuf_large_pool_size', 1024))},
                dataEngineIobufSmallPoolSize={'v2': str(c.get('longhorn_v2_iobuf_small_pool_size', 8192))})
        if c.get('longhorn_v2_rebuild_bandwidth_limit') is not None:
            values['defaultSettings']['replicaRebuildingBandwidthLimit'] = {
                'v2': str(int(c['longhorn_v2_rebuild_bandwidth_limit']))}
    return values


class CommandError(RuntimeError):
    """Keep native error text in memory for classification, out of public logs."""
    def __init__(self, command, returncode, stderr):
        super().__init__('Longhorn command failed: ' + command + ' rc=' + str(returncode))
        self.returncode = returncode
        self.stderr = stderr


def run(argv, *, stdin=None, timeout=120):
    # Keep potentially sensitive CLI output private. Temporary files avoid pipe
    # backpressure and timeout heartbeats expose only the executable name.
    with tempfile.TemporaryFile(mode='w+') as output, tempfile.TemporaryFile(mode='w+') as error:
        with tempfile.TemporaryFile(mode='w+') as input_file:
            if stdin is not None:
                input_file.write(stdin)
            input_file.seek(0)
            process = subprocess.Popen([str(x) for x in argv], stdin=input_file, stdout=output, stderr=error)
            started = time.monotonic()
            try:
                while True:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError('Longhorn command timeout: ' + Path(argv[0]).name)
                    try:
                        rc = process.wait(timeout=min(15, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        print('LONGHORN_COMMAND_HEARTBEAT command=' + Path(argv[0]).name +
                              ' elapsed=' + str(int(time.monotonic() - started)), flush=True)
                if rc:
                    error.seek(0)
                    raise CommandError(Path(argv[0]).name, rc, error.read())
                output.seek(0)
                return output.read()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class API:
    def __init__(self, kubeconfig):
        self.prefix = ['kubectl', '--kubeconfig=' + kubeconfig, '--request-timeout=30s']

    def get(self, kind, object_name='', namespace=''):
        scope = ['-A'] if namespace == '*' else (['-n', namespace] if namespace else [])
        argv = self.prefix + ['get', kind] + scope
        if object_name:
            argv += [object_name, '--ignore-not-found']
        output = run(argv + ['-o', 'json'])
        return json.loads(output) if output.strip() else None

    def apply(self, obj, *, create=False):
        return json.loads(run(self.prefix + ['create' if create else 'apply'] +
                             ([] if create else ['--server-side', '--field-manager=kubeauto-longhorn']) +
                             ['-f', '-', '-o', 'json'], stdin=json.dumps(obj)))

    def replace(self, obj):
        return json.loads(run(self.prefix + ['replace', '-f', '-', '-o', 'json'], stdin=json.dumps(obj)))

    def patch(self, kind, object_name, patch, namespace=''):
        return json.loads(run(self.prefix + (['-n', namespace] if namespace else []) +
                              ['patch', kind, object_name, '--type=merge', '--patch-file=/dev/stdin', '-o', 'json'],
                              stdin=json.dumps(patch)))

    def delete(self, obj, *, propagation_policy=None):
        meta = obj['metadata']
        # kubectl delete has no UID precondition flag; raw DELETE uses the API
        # object's own selfLink-equivalent path and explicit DeleteOptions.
        group = obj['apiVersion']
        root = '/api/' if '/' not in group else '/apis/'
        discovery = json.loads(run(self.prefix + ['get', '--raw', root + group]))
        matches = [r['name'] for r in discovery['resources'] if r['kind'] == obj['kind'] and '/' not in r['name']]
        if len(matches) != 1:
            raise ValueError('cannot identify the resource for guarded deletion')
        path = root + group + ('/namespaces/' + meta['namespace'] if meta.get('namespace') else '') + '/' + matches[0] + '/' + meta['name']
        options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {
            'uid': meta['uid'], 'resourceVersion': meta['resourceVersion']}}
        if propagation_policy is not None:
            options['propagationPolicy'] = propagation_policy
        run(self.prefix + ['delete', '--raw', path, '-f', '-'], stdin=json.dumps(options))


def wait(label, timeout, check):
    start = time.monotonic()
    while True:
        value = check()
        if value:
            return value
        elapsed = int(time.monotonic() - start)
        if elapsed >= timeout:
            raise TimeoutError('Longhorn timeout: ' + label)
        print('LONGHORN_WAIT_HEARTBEAT stage=' + label + ' elapsed=' + str(elapsed), flush=True)
        time.sleep(min(10, timeout - elapsed))


def assert_helm_owner(obj):
    require_owned(obj)
    if obj:
        annotations = obj.get('metadata', {}).get('annotations', {})
        if annotations.get('meta.helm.sh/release-name') != 'longhorn' or annotations.get('meta.helm.sh/release-namespace') != NAMESPACE:
            raise ValueError('resource belongs to another Helm release')


def verify_grpc_tls(directory):
    """Verify the shared gRPC identity with native OpenSSL via Python's SSL API.

    MemoryBIO performs a real mutual TLS handshake without sockets or an
    openssl executable in the isolated Ansible execution environment.
    """
    directory = Path(directory)
    client = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(directory/'ca.crt'))
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=str(directory/'ca.crt'))
    server.verify_mode = ssl.CERT_REQUIRED
    for context in (client, server):
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        # An empty password prevents an interactive prompt for encrypted keys.
        context.load_cert_chain(str(directory/'tls.crt'), str(directory/'tls.key'), password='')
    hosts = [host + suffix for host in ('longhorn-backend', 'longhorn-frontend', 'longhorn-engine-manager',
             'longhorn-replica-manager', 'longhorn-csi') for suffix in ('', '.longhorn-system', '.longhorn-system.svc')]
    for host in [*hosts, '127.0.0.1']:
        client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
        peers = [client.wrap_bio(client_in, client_out, server_hostname=host),
                 server.wrap_bio(server_in, server_out, server_side=True)]
        completed = [False, False]
        for _ in range(64):
            for index, peer in enumerate(peers):
                if not completed[index]:
                    try:
                        peer.do_handshake()
                        completed[index] = True
                    except ssl.SSLWantReadError:
                        pass
            for outgoing, incoming in ((client_out, server_in), (server_out, client_in)):
                if outgoing.pending:
                    incoming.write(outgoing.read())
            if all(completed):
                break
        else:
            raise ValueError('Longhorn gRPC TLS handshake did not complete')
        if ssl.cert_time_to_seconds(peers[0].getpeercert()['notAfter']) < time.time() + 86400:
            raise ValueError('Longhorn gRPC certificate expires within 24 hours')


def check_secrets(c, api):
    def secret(object_name, keys):
        obj = api.get('secret', object_name, NAMESPACE)
        if not obj or any(not obj.get('data', {}).get(k) for k in keys):
            raise ValueError('missing Longhorn credential keys: ' + object_name)
        return obj
    if flag(c['longhorn_grpc_tls_required']):
        obj = secret('longhorn-grpc-tls', ('ca.crt', 'tls.crt', 'tls.key'))
        with tempfile.TemporaryDirectory(prefix='longhorn-tls-') as directory:
            for key in ('ca.crt', 'tls.crt', 'tls.key'):
                p = Path(directory) / key
                p.write_bytes(base64.b64decode(obj['data'][key], validate=True)); p.chmod(0o600)
            verify_grpc_tls(directory)
    for sc in c['longhorn_storage_classes']:
        if flag(sc['encrypted']):
            secret(sc['encryption_secret'], ('CRYPTO_KEY_VALUE', 'CRYPTO_KEY_PROVIDER'))
    if c['longhorn_backup_target'].startswith('s3://'):
        obj = secret(c['longhorn_backup_secret'], ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'))
        endpoint = obj.get('data', {}).get('AWS_ENDPOINTS')
        if endpoint and not base64.b64decode(endpoint, validate=True).decode().startswith('https://'):
            raise ValueError('Longhorn S3 endpoint must use HTTPS')


def node_map(c, api):
    nodes = api.get('nodes')['items']
    selected = {}
    for address in c['longhorn_inventory_nodes']:
        matches = [n for n in nodes if any(a.get('type') == 'InternalIP' and a.get('address') == address for a in n.get('status', {}).get('addresses', []))]
        if len(matches) != 1:
            raise ValueError('inventory IP does not identify one Kubernetes Node: ' + address)
        node = matches[0]
        if node.get('metadata', {}).get('labels', {}).get('kubernetes.io/os') != 'linux' or not any(x['type'] == 'Ready' and x['status'] == 'True' for x in node.get('status', {}).get('conditions', [])):
            raise ValueError('Longhorn client node is not Ready/Linux: ' + address)
        selected[address] = node
        if address in c.get('longhorn_v2_nodes', []):
            if node.get('spec', {}).get('unschedulable'):
                raise ValueError('V2-enabled client node must be schedulable before installation')
            check_v2_resources(c, node)
    return selected


def check_classes(c, api):
    existing = api.get('storageclasses')['items']
    for sc in c['longhorn_storage_classes']:
        desired = storage_class(sc)
        current = next((x for x in existing if x['metadata']['name'] == sc['name']), None)
        require_owned(current)
        if current and any(current.get(k) != desired.get(k) for k in ('provisioner', 'parameters', 'reclaimPolicy', 'volumeBindingMode')):
            raise ValueError('immutable StorageClass change requires a new class name: ' + sc['name'])
        if flag(sc['default']) and any(x['metadata']['name'] != sc['name'] and x.get('metadata', {}).get('annotations', {}).get('storageclass.kubernetes.io/is-default-class') == 'true' for x in existing):
            raise ValueError('refusing to replace another default StorageClass')


def host_context(c, api):
    """Bind host receipts to live API identities, including during uninstall."""
    system = api.get('namespace', 'kube-system')
    if not system or not system.get('metadata', {}).get('uid'):
        raise ValueError('missing live cluster identity for host filesystem receipts')
    nodes = api.get('nodes')['items']; identities = {}
    for address in set(c['longhorn_storage_nodes']) | set(c.get('longhorn_v2_nodes', [])):
        matches = [n for n in nodes if any(a.get('type') == 'InternalIP' and a.get('address') == address
                   for a in n.get('status', {}).get('addresses', []))]
        if len(matches) != 1 or not matches[0].get('metadata', {}).get('uid'):
            raise ValueError('storage host requires a unique live Kubernetes node identity')
        identities[address] = matches[0]['metadata']['uid']
    return {'cluster_uid': system['metadata']['uid'], 'node_uids': identities}


def block_retirement():
    spec = importlib.util.spec_from_file_location('longhorn_block_retirement', FILES/'block-retire.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def preflight(c, api, helm, values):
    ns = api.get('namespace', NAMESPACE)
    require_owned(ns)
    releases = json.loads(run([helm, 'list', '--all', '--all-namespaces', '-o', 'json']))
    current = None
    for release in releases:
        if release['chart'].startswith('longhorn-'):
            if (release['name'], release['namespace']) != ('longhorn', NAMESPACE):
                raise ValueError('another Longhorn Helm installation exists')
            current = release
    check_installation_identity(api, values, current)
    if c['longhorn_action'] == 'uninstall':
        if current:
            empty_for_uninstall(api)
            if api.get('crd', 'volumesnapshotclasses.snapshot.storage.k8s.io'):
                require_owned(api.get('volumesnapshotclass', 'longhorn-snapshot'))
            require_owned(api.get('networkpolicy', 'kubeauto-longhorn-metrics', NAMESPACE))
        else:
            guard(api)
        return {}, current
    if c['longhorn_action'] == 'retire-block-disks':
        if not current or current['app_version'].removeprefix('v') != '1.12.1':
            raise ValueError('block retirement requires the accepted Longhorn 1.12.1 installation')
        nodes = node_map(c, api)
        block_retirement().preflight(c, api, values, sys.modules[__name__])
        return nodes, current
    nodes = node_map(c, api)
    check_secrets(c, api)
    check_classes(c, api)
    snapshot_preflight(c, api)
    check_declarations(c, api)
    check_recurring_identity(c, api, values)
    check_engine_changes(c, api, nodes)
    if current:
        old = current['app_version'].removeprefix('v')
        if old not in CHART_HASHES or (old == '1.12.1' and c['longhorn_version'] == '1.12.0'):
            raise ValueError('unsupported Longhorn upgrade or direct downgrade')
        if old != c['longhorn_version']:
            if not c['longhorn_upgrade_id'] or not c['longhorn_upgrade_system_backup']:
                raise ValueError('Longhorn upgrade requires change ID and verified system backup')
            backup = api.get('systembackups.longhorn.io', c['longhorn_upgrade_system_backup'], NAMESPACE)
            if not backup or backup.get('status', {}).get('state') != 'Ready' or backup.get('status', {}).get('version', '').removeprefix('v') != old:
                raise ValueError('Longhorn upgrade system backup is not Ready for the source version')
            for volume in api.get('volumes.longhorn.io', namespace=NAMESPACE)['items']:
                spec, status = volume.get('spec', {}), volume.get('status', {})
                if spec.get('dataEngine', 'v1') not in engines(c):
                    raise ValueError('Longhorn upgrade cannot disable the volume data engine')
                if status.get('robustness') not in ('healthy', 'unknown'):
                    raise ValueError('Longhorn upgrade requires healthy volumes')
                if spec.get('dataEngine', 'v1') == 'v2' and (
                        status.get('state') != 'detached' or spec.get('nodeID') or spec.get('migrationNodeID')):
                    raise ValueError('Longhorn V2 patch upgrade requires detached volumes')
                if spec.get('dataEngine') == 'v2' and spec.get('backingImage'):
                    raise ValueError('Longhorn V2 backing images require migration before 1.12.1 upgrade')
            v2_names = {v['metadata']['name'] for v in api.get('volumes.longhorn.io', namespace=NAMESPACE)['items']
                        if v.get('spec', {}).get('dataEngine') == 'v2'}
            if v2_names:
                for plural in ('engines', 'replicas'):
                    if any(obj.get('spec', {}).get('volumeName') in v2_names and
                           obj.get('status', {}).get('currentState') != 'stopped'
                           for obj in api.get(plural + '.longhorn.io', namespace=NAMESPACE)['items']):
                        raise ValueError('V2 upgrade requires stopped engine and replica instances')
    chart = FILES / ('longhorn-' + c['longhorn_version'] + '.tgz')
    if hashlib.sha256(chart.read_bytes()).hexdigest() != CHART_HASHES[c['longhorn_version']]:
        raise ValueError('Longhorn official Chart checksum mismatch')
    manifest = run([helm, 'template', 'longhorn', chart, '-n', NAMESPACE, '-f', values])
    for obj in yaml.safe_load_all(manifest):
        if obj:
            meta = obj['metadata']
            existing = api.get(obj['kind'], meta['name'], meta.get('namespace', ''))
            assert_helm_owner(existing)
    check_disk_declarations(c, api, nodes)
    check_block_identity(c, api, nodes, values)
    for node in nodes.values():
        meta = node['metadata']
        label = meta.get('labels', {}).get('node.longhorn.io/create-default-disk')
        if (label or any(k in meta.get('labels', {}) for k in
                ('kubeauto.io/longhorn-client', 'node.longhorn.io/disable-v2-data-engine'))) and not owned(node):
            raise ValueError('foreign Longhorn disk bootstrap label')
    return nodes, current


def block_declarations(c, address):
    return {disk['name']: {'path': disk['path'], 'diskType': 'block', 'diskDriver': disk['disk_driver'],
            'allowScheduling': flag(disk.get('allow_scheduling', True)),
            'evictionRequested': flag(disk.get('eviction_requested', False)),
            'storageReserved': int(disk['storage_reserved']), 'tags': []}
            for disk in c.get('longhorn_block_disks', {}).get(address, [])}


def check_disk_scheduling(current, desired):
    if current.get('evictionRequested') and desired['allowScheduling']:
        raise ValueError('cancel Longhorn disk eviction while scheduling remains disabled before re-enabling it')


def block_disk_ready(disk, state):
    required = ('Ready', 'Schedulable') if disk.get('allowScheduling', True) else ('Ready',)
    return all(any(condition.get('type') == kind and condition.get('status') == 'True'
                   for condition in state.get('conditions', [])) for kind in required)


def block_disk_drained(api, state):
    """Observe native evacuation; never delete replicas or their on-disk data."""
    if not state.get('diskUUID') or any(state.get(key) for key in (
            'storageScheduled', 'scheduledReplica', 'scheduledBackingImage')):
        return False
    return not any(replica.get('spec', {}).get('diskID') == state['diskUUID']
                   for replica in api.get('replicas.longhorn.io', namespace=NAMESPACE)['items'])


def check_engine_changes(c, api, nodes):
    if not api.get('crd', 'volumes.longhorn.io'):
        return
    volumes = api.get('volumes.longhorn.io', namespace=NAMESPACE)['items']
    for volume in volumes:
        if volume.get('spec', {}).get('dataEngine', 'v1') not in engines(c):
            raise ValueError('cannot disable an engine while its volumes remain')
    v2_names = {nodes[a]['metadata']['name'] for a in c.get('longhorn_v2_nodes', [])}
    exclusive = int(c.get('longhorn_v2_cpu_cores', 0))
    reactor_mcpu = (exclusive or int(c.get('longhorn_v2_cpu_mask', '0x3'), 16).bit_count()) * 1000
    by_name = {node['metadata']['name']: node for node in nodes.values()}
    for obj in api.get('nodes.longhorn.io', namespace=NAMESPACE)['items']:
        if exclusive and obj.get('status', {}).get('cpuPolicy') != 'static':
            raise ValueError('exclusive CPU allocation requires static CPU policy on every Longhorn node')
        request = obj.get('spec', {}).get('instanceManagerCPURequest', 0)
        if obj['metadata']['name'] in v2_names:
            if exclusive:
                check_exclusive_cpu_budget(c, by_name[obj['metadata']['name']], request)
            elif request and request < reactor_mcpu:
                raise ValueError('V2 node CPU override is below SPDK reactor requirements')
    for im in api.get('instancemanagers.longhorn.io', namespace=NAMESPACE)['items']:
        if im.get('spec', {}).get('dataEngine') == 'v2' and im['spec'].get('nodeID') in v2_names:
            override = im['spec'].get('dataEngineSpec', {}).get('v2', {}).get('cpuMask', '')
            if not exclusive and override and int(override, 16) != int(c['longhorn_v2_cpu_mask'], 16):
                raise ValueError('V2 per-instance CPU mask differs from the qualified global declaration')
        if im.get('spec', {}).get('dataEngine') == 'v2' and im['spec'].get('nodeID') not in v2_names:
            status = im.get('status', {})
            if status.get('instances') or status.get('instanceEngines') or status.get('instanceReplicas'):
                raise ValueError('cannot disable a V2 node with active native instances')
    if not any(v.get('spec', {}).get('dataEngine') == 'v2' and
               (v.get('status', {}).get('state') != 'detached' or v.get('spec', {}).get('nodeID')) for v in volumes):
        return
    desired = chart_values(c)['defaultSettings']
    keys = {'dataEngineCPUMask': 'data-engine-cpu-mask',
            'dataEngineNumberOfCPUCores': 'data-engine-number-of-cpu-cores',
            'dataEngineIobufLargePoolSize': 'data-engine-iobuf-large-pool-size',
            'dataEngineIobufSmallPoolSize': 'data-engine-iobuf-small-pool-size',
            'guaranteedInstanceManagerCPU': 'guaranteed-instance-manager-cpu',
            'dataEngineMemorySize': 'data-engine-memory-size',
            'dataEngineHugepageEnabled': 'data-engine-hugepage-enabled',
            'dataEngineInterruptModeEnabled': 'data-engine-interrupt-mode-enabled',
            'dataEngineCPUIsolationEnabled': 'data-engine-cpu-isolation-enabled'}
    for key, setting in keys.items():
        if key not in desired:
            continue
        actual = api.get('settings.longhorn.io', setting, NAMESPACE)
        if not actual or json.loads(actual['value']).get('v2') != desired[key]['v2']:
            raise ValueError('V2 resource-setting changes require all V2 volumes detached: ' + setting)


def check_disk_declarations(c, api, nodes):
    if not api.get('crd', 'nodes.longhorn.io'):
        return
    addresses = {node['metadata']['name']: address for address, node in nodes.items()}
    for existing in api.get('nodes.longhorn.io', namespace=NAMESPACE)['items']:
        disks = existing.get('spec', {}).get('disks', {})
        if not disks:
            continue
        # The bootstrap annotation is consumed only when the first disk is
        # created. Native multi-disk maintenance must survive reconciliation;
        # extra disks stay administrator-managed and are never overwritten.
        address = addresses.get(existing['metadata']['name'])
        filesystem = {key: disk for key, disk in disks.items() if disk.get('diskType', 'filesystem') == 'filesystem'}
        if filesystem and (address not in c['longhorn_storage_nodes'] or
                sum(disk.get('path') == c['longhorn_data_path'] for disk in filesystem.values()) != 1):
            raise ValueError('existing Longhorn disks differ from the V1 storage allowlist; evacuate explicitly')
        declared = block_declarations(c, address)
        maintenance = {d['name'] for d in c.get('longhorn_block_disks', {}).get(address, [])
                       if 'allow_scheduling' in d}
        for key, disk in disks.items():
            if disk.get('diskType', 'filesystem') not in ('filesystem', 'block'):
                raise ValueError('unknown Longhorn native disk type')
            if disk.get('diskType') == 'block' and (key not in declared or any(
                    disk.get(field) != declared[key][field] for field in ('path', 'diskType', 'diskDriver'))):
                raise ValueError('existing Longhorn block disks differ from the V2 allowlist; evacuate explicitly')
            if key in declared and any(disk.get(field) != declared[key][field] for field in ('path', 'diskType', 'diskDriver')):
                raise ValueError('existing disk name collides with the declared V2 block device')
            if key in maintenance:
                check_disk_scheduling(disk, declared[key])


def reconcile_filesystem_disks(c, api, nodes):
    """Add a verified V1 filesystem when upstream bootstrap skips existing disks."""
    for address in c['longhorn_storage_nodes']:
        receipt = c['longhorn_disk_receipts'][address]
        node_name = nodes[address]['metadata']['name']
        identities = c.setdefault('_filesystem_node_uids', {})
        for attempt in range(3):
            current = api.get('nodes.longhorn.io', node_name, NAMESPACE)
            if not current:
                return False
            uid = current['metadata']['uid']
            if identities.setdefault(node_name, uid) != uid:
                raise ValueError('V1 native Node identity changed during disk reconciliation')
            disks = current.get('spec', {}).get('disks', {})
            if not disks:
                break  # Initial default-disk creation remains upstream-owned.
            matches = [d for d in disks.values() if d.get('path') == receipt['path']]
            if matches:
                if len(matches) != 1 or matches[0].get('diskType', 'filesystem') != 'filesystem':
                    raise ValueError('V1 filesystem path belongs to a different native disk')
                break
            if 'kubeauto-data' in disks or any(d.get('diskType', 'filesystem') == 'filesystem' for d in disks.values()):
                raise ValueError('V1 disk declaration differs from the verified filesystem')
            desired = {'path': receipt['path'], 'diskType': 'filesystem', 'allowScheduling': True,
                       'evictionRequested': False, 'storageReserved': int(receipt['total_bytes']) // 10,
                       'tags': []}
            try:
                api.patch('nodes.longhorn.io', node_name, {'metadata': {
                    'uid': uid, 'resourceVersion': current['metadata']['resourceVersion']},
                    'spec': {'disks': {'kubeauto-data': desired}}}, NAMESPACE)
                break
            except CommandError as error:
                if not error.stderr.startswith('Error from server (Conflict):'):
                    raise
                if attempt == 2:
                    raise ValueError('V1 disk declaration conflict retry limit reached') from None
    return True


def reconcile_block_disks(c, api, nodes):
    """Add approved disks; only explicitly declared maintenance flags converge."""
    for address in c.get('longhorn_block_disks', {}):
        node_name = nodes[address]['metadata']['name']
        declared = block_declarations(c, address)
        maintenance = {d['name'] for d in c['longhorn_block_disks'][address] if 'allow_scheduling' in d}
        identities = c.setdefault('_block_node_uids', {})
        for attempt in range(3):
            current = api.get('nodes.longhorn.io', node_name, NAMESPACE)
            if not current:
                return False
            uid = current['metadata']['uid']
            if identities.setdefault(node_name, uid) != uid:
                raise ValueError('V2 native Node identity changed during disk reconciliation')
            disks = current.get('spec', {}).get('disks', {})
            for key, desired in declared.items():
                if key in disks and any(disks[key].get(field) != desired[field] for field in ('path', 'diskType', 'diskDriver')):
                    raise ValueError('V2 native disk identity changed during reconciliation')
                if key not in disks and any(disk.get('path') == desired['path'] for disk in disks.values()):
                    raise ValueError('V2 device path already belongs to another native disk')
            additions = {key: disk for key, disk in declared.items() if key not in disks}
            for key in maintenance & disks.keys():
                check_disk_scheduling(disks[key], declared[key])
                flags = {field: declared[key][field] for field in ('allowScheduling', 'evictionRequested')}
                if any(disks[key].get(field) != value for field, value in flags.items()):
                    additions[key] = flags
            if not additions:
                break
            try:
                api.patch('nodes.longhorn.io', node_name, {'metadata': {
                    'uid': current['metadata']['uid'], 'resourceVersion': current['metadata']['resourceVersion']},
                    'spec': {'disks': additions}}, NAMESPACE)
                break
            except CommandError as error:
                if not error.stderr.startswith('Error from server (Conflict):'):
                    raise
                if attempt == 2:
                    raise ValueError('V2 disk declaration conflict retry limit reached') from None
    return True


def check_block_receipts(c, context):
    receipts = c.get('longhorn_block_receipts', {})
    if set(receipts) != set(c.get('longhorn_v2_nodes', [])):
        raise ValueError('missing current V2 host qualification receipts')
    for address, receipt in receipts.items():
        if any(receipt.get(key) != value for key, value in (
                ('cluster_uid', context['cluster_uid']), ('node_uid', context['node_uids'][address]))):
            raise ValueError('V2 host receipt belongs to another cluster/node')
        declared = {disk['name']: disk for disk in c.get('longhorn_block_disks', {}).get(address, [])}
        if set(receipt.get('disks', {})) != set(declared):
            raise ValueError('V2 host receipt does not match declared raw disks')
        for key, desired in declared.items():
            actual = receipt['disks'][key]
            if (any(actual.get(field) != desired[field] for field in ('path', 'disk_driver', 'identity')) or
                    not actual.get('size_bytes') or int(desired['storage_reserved']) >= actual['size_bytes'] * 3 // 4):
                raise ValueError('V2 raw disk receipt identity/capacity mismatch')
        settings = receipt.get('platform', {})
        if any(settings.get(key) != value for key, value in (
                ('cpu_mask', c['longhorn_v2_cpu_mask']), ('memory_mib', int(c['longhorn_v2_memory_mib'])),
                ('hugepages', flag(c['longhorn_v2_hugepages'])))):
            raise ValueError('V2 host receipt resource settings differ from requested SPDK settings')
        if settings.get('cpu_cores', 0) != int(c.get('longhorn_v2_cpu_cores', 0)):
            raise ValueError('V2 host receipt exclusive CPU count differs from requested settings')


def check_block_identity(c, api, nodes, values, *, record=False):
    """Bind native block-disk UUIDs to the installation and approved hardware.

    A Ready disk at the same path must not silently replace an accepted lvstore.
    Missing native identities after acceptance require explicit recovery.
    """
    path = Path(values).with_name('block-disks.json')
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('invalid Longhorn native block identity receipt')
    old = json.loads(path.read_text()) if path.exists() else None
    if not old and (not record or not c.get('longhorn_block_disks')):
        return
    installation = json.loads(Path(values).with_name('installation.json').read_text())
    identity = {key: installation[key] for key in ('cluster_uid', 'namespace_uid')}
    if old and any(old.get(key) != value for key, value in identity.items()):
        raise ValueError('native block receipt belongs to another installation')
    accepted = old.get('nodes', {}) if old else {}
    declared = c.get('longhorn_block_disks', {})
    if not set(accepted) <= set(declared):
        raise ValueError('native block node retirement requires explicit evacuation and release')
    result = dict(identity, nodes={})
    uuids = set()
    for address, declarations in declared.items():
        expected = nodes[address]['metadata']
        current = api.get('nodes.longhorn.io', expected['name'], NAMESPACE)
        previous = accepted.get(address, {})
        observed_uid = c.get('_block_node_uids', {}).get(expected['name'])
        if current and observed_uid and current['metadata']['uid'] != observed_uid:
            raise ValueError('native block Node changed after disk reconciliation')
        if not current or current['metadata'].get('deletionTimestamp'):
            if previous or record:
                raise ValueError('accepted native block Node disappeared or is being deleted')
            continue
        entry = {'name': expected['name'], 'kubernetes_node_uid': expected['uid'],
                 'longhorn_node_uid': current['metadata']['uid'], 'disks': {}}
        if previous and any(previous.get(key) != entry[key] for key in entry if key != 'disks'):
            raise ValueError('accepted native block Node identity changed')
        requested = {disk['name']: disk for disk in declarations}
        if not set(previous.get('disks', {})) <= set(requested):
            raise ValueError('native block disk retirement requires explicit evacuation and release')
        for name, disk in requested.items():
            native = current.get('spec', {}).get('disks', {}).get(name)
            status = (current.get('status', {}).get('diskStatus') or {}).get(name) or {}
            known = previous.get('disks', {}).get(name)
            if not known and not record:
                continue
            if (not native or native.get('diskType') != 'block' or
                    native.get('path') != disk['path'] or native.get('diskDriver') != disk['disk_driver'] or
                    not status.get('diskUUID') or status.get('diskType') != 'block' or
                    status.get('diskDriver') != disk['disk_driver']):
                raise ValueError('accepted native block disk identity is unavailable or changed')
            if record and not block_disk_ready(native, status):
                raise ValueError('native block identity requires Ready and, while scheduling is enabled, Schedulable')
            if status['diskUUID'] in uuids:
                raise ValueError('duplicate native block disk UUID')
            uuids.add(status['diskUUID'])
            entry['disks'][name] = {'disk_uuid': status['diskUUID'], 'path': disk['path'],
                                   'disk_driver': disk['disk_driver'], 'hardware_identity': disk['identity']}
            if known and known != entry['disks'][name]:
                raise ValueError('native block disk UUID or hardware declaration changed')
        result['nodes'][address] = entry
    if record and result != old:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(result, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def retire_block_identity(values):
    """Archive accepted native identities only after the uninstall guard is empty."""
    path = Path(values).with_name('block-disks.json')
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError('invalid Longhorn native block identity receipt')
    row = json.loads(path.read_text())
    installation = json.loads(Path(values).with_name('installation.json').read_text())
    if any(row.get(key) != installation[key] for key in ('cluster_uid', 'namespace_uid')):
        raise ValueError('native block receipt belongs to another installation')
    archive = path.with_name('block-disks-uninstalled-' + row['namespace_uid'] + '.json')
    if archive.exists() or archive.is_symlink():
        raise ValueError('native block uninstall archive already exists')
    path.rename(archive)


def contains(actual, desired):
    """Compare declarations while allowing API-server defaulted fields."""
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k, v in desired.items())
    if isinstance(desired, list):
        return isinstance(actual, list) and len(actual) == len(desired) and all(contains(a, b) for a, b in zip(actual, desired))
    return actual == desired


def snapshot_objects(c):
    directory = FILES / 'snapshot-controller-8.6.0'
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        digest, filename = line.split()
        if hashlib.sha256((directory / filename).read_bytes()).hexdigest() != digest:
            raise ValueError('official snapshot-controller file checksum mismatch')
    objects = []
    def rename(value):
        if isinstance(value, dict):
            return {k: rename(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rename(x) for x in value]
        if isinstance(value, str) and value.startswith('snapshot-controller'):
            return 'kubeauto-' + value
        return value
    for path in sorted(directory.glob('*.yaml')):
        for source in yaml.safe_load_all(path.read_text()):
            if not source:
                continue
            obj = rename(source)
            obj['metadata'].setdefault('labels', {}).update(SNAPSHOT_OWNER)
            if obj['kind'] == 'Deployment':
                container = obj['spec']['template']['spec']['containers'][0]
                container['image'] = c['longhorn_image_prefix'] + '/snapshot-controller:v8.6.0'
                container['args'].append('--prevent-volume-mode-conversion=true')
                container['resources'] = {'requests': {'cpu': '100m', 'memory': '128Mi'},
                                          'limits': {'cpu': '1', 'memory': '512Mi'}}
            objects.append(obj)
    return objects


def snapshot_preflight(c, api):
    mode = c['longhorn_snapshot_controller']
    if mode == 'disabled':
        return []
    objects = snapshot_objects(c)
    controllers = []
    for deployment in api.get('deployments', namespace='*')['items']:
        containers = deployment.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
        if any('snapshot-controller' in x.get('name', '') or '/snapshot-controller:' in x.get('image', '') for x in containers):
            controllers.append(deployment)
    if mode == 'external':
        if len(controllers) != 1:
            raise ValueError('external snapshot mode requires one unique controller deployment')
        deployment = controllers[0]
        if deployment.get('status', {}).get('availableReplicas', 0) < 1 or not any(x.get('image', '').endswith('/snapshot-controller:v8.6.0') for x in deployment['spec']['template']['spec']['containers']):
            raise ValueError('external snapshot controller must be healthy and pinned to v8.6.0')
    elif len(controllers) > 1 or any(not owned(x, SNAPSHOT_OWNER) or
            (x['metadata']['name'], x['metadata'].get('namespace')) != ('kubeauto-snapshot-controller', 'kube-system')
            for x in controllers):
        raise ValueError('refusing a second or foreign snapshot controller')
    for obj in objects:
        if mode == 'external' and obj['kind'] != 'CustomResourceDefinition':
            continue
        meta = obj['metadata']
        current = api.get(obj['kind'], meta['name'], meta.get('namespace', ''))
        if mode == 'managed':
            require_owned(current, SNAPSHOT_OWNER)
            if current and obj['kind'] == 'CustomResourceDefinition' and not contains(current['spec'], obj['spec']):
                raise ValueError('managed snapshot CRD differs from the pinned schema')
        elif not current or not contains(current['spec'], obj['spec']) or not any(x.get('type') == 'Established' and x.get('status') == 'True' for x in current.get('status', {}).get('conditions', [])):
            raise ValueError('external snapshot CRD schema/establishment differs from v8.6.0')
    return objects if mode == 'managed' else []


def snapshot_class():
    obj = resource('VolumeSnapshotClass', 'longhorn-snapshot', api='snapshot.storage.k8s.io/v1', namespace='')
    obj.update(driver='driver.longhorn.io', deletionPolicy='Retain', parameters={'type': 'snap'})
    return obj


def metrics_policy(c):
    return resource('NetworkPolicy', 'kubeauto-longhorn-metrics', {
        'podSelector': {'matchLabels': {'app': 'longhorn-manager'}}, 'policyTypes': ['Ingress'],
        'ingress': [{'from': [{'namespaceSelector': {'matchLabels': {
            'kubernetes.io/metadata.name': c['longhorn_monitoring_namespace']}},
            'podSelector': {'matchLabels': c['longhorn_monitoring_selector']}}],
            'ports': [{'port': 9500, 'protocol': 'TCP'}]}]}, api='networking.k8s.io/v1')


def check_request(current, desired):
    require_owned(current)
    if current and (current.get('metadata', {}).get('annotations', {}).get('kubeauto.io/request-sha256') !=
                    desired['metadata']['annotations']['kubeauto.io/request-sha256'] or
                    not contains(current.get('spec', {}), desired['spec'])):
        raise ValueError('immutable Longhorn request differs; use a new name')


def check_declarations(c, api):
    crds = {x['metadata']['name'] for x in api.get('crds')['items']}
    if c['longhorn_snapshot_controller'] != 'disabled' and 'volumesnapshotclasses.snapshot.storage.k8s.io' in crds:
        desired = snapshot_class()
        current = api.get('volumesnapshotclass', desired['metadata']['name'])
        require_owned(current)
        if current and any(current.get(k) != desired[k] for k in ('driver', 'deletionPolicy', 'parameters')):
            raise ValueError('immutable VolumeSnapshotClass change requires a new class name')
    for job in c['longhorn_recurring_jobs']:
        if 'recurringjobs.longhorn.io' in crds:
            require_owned(api.get('recurringjobs.longhorn.io', job['name'], NAMESPACE))
    for request in c['longhorn_requests']:
        desired = request_object(request)
        if desired['kind'].lower() + 's.longhorn.io' in crds:
            check_request(api.get(desired['kind'] + '.longhorn.io', request['name'], NAMESPACE), desired)
    require_owned(api.get('networkpolicy', 'kubeauto-longhorn-metrics', NAMESPACE))
    if flag(c['longhorn_monitoring_enabled']) and 'servicemonitors.monitoring.coreos.com' not in crds:
        raise ValueError('monitoring requires the Prometheus Operator ServiceMonitor CRD')


def check_installation_identity(api, values, release):
    receipt = Path(values).with_name('installation.json')
    namespace = api.get('namespace', NAMESPACE)
    if receipt.exists():
        old = json.loads(receipt.read_text())
        cluster = api.get('namespace', 'kube-system')
        if old['cluster_uid'] != cluster['metadata']['uid'] or not namespace or old['namespace_uid'] != namespace['metadata']['uid']:
            raise ValueError('Longhorn installation identity changed; refuse adopting another cluster or namespace')
    elif release or any(x['metadata']['name'].endswith('.longhorn.io') for x in api.get('crds')['items']):
        raise ValueError('existing Longhorn release or CRDs have no local installation receipt; adoption is not supported')
    elif api.get('csidriver', 'driver.longhorn.io'):
        raise ValueError('existing Longhorn CSI driver has no local installation receipt; adoption is not supported')


def record_installation(api, values):
    receipt = Path(values).with_name('installation.json')
    if not receipt.exists():
        data = {'cluster_uid': api.get('namespace', 'kube-system')['metadata']['uid'],
                'namespace_uid': api.get('namespace', NAMESPACE)['metadata']['uid']}
        # The directory is private. Exclusive creation never overwrites an identity.
        with receipt.open('x') as stream:
            os.chmod(receipt, 0o600)
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())


def rebuild_qos_ready(c, api):
    """Setting convergence only; active rebuilds report their own applied QoS."""
    bandwidth = c.get('longhorn_v2_rebuild_bandwidth_limit')
    if bandwidth is None:
        return True
    current = api.get('settings.longhorn.io', 'replica-rebuilding-bandwidth-limit', NAMESPACE)
    return bool(current and json.loads(current['value']).get('v2') == str(int(bandwidth)))


def healthy(c, api, nodes):
    clients = {address: nodes[address] for address in c['longhorn_client_nodes']}
    for kind, object_name in (('daemonset', 'longhorn-manager'), ('daemonset', 'longhorn-csi-plugin')):
        obj = api.get(kind, object_name, NAMESPACE)
        if not obj:
            return False
        status = obj.get('status', {})
        expected = len(clients) if object_name == 'longhorn-manager' else len(nodes)
        if status.get('observedGeneration', 0) < obj['metadata'].get('generation', 1) or status.get('numberReady', 0) < expected or status.get('updatedNumberScheduled', 0) < expected or status.get('numberUnavailable', 0):
            return False
    for object_name in ('csi-attacher', 'csi-provisioner', 'csi-resizer', 'csi-snapshotter', 'longhorn-ui'):
        deployment = api.get('deployment', object_name, NAMESPACE)
        if not deployment or deployment.get('status', {}).get('availableReplicas', 0) < deployment.get('spec', {}).get('replicas', 1) or deployment.get('status', {}).get('updatedReplicas', 0) != deployment.get('spec', {}).get('replicas', 1):
            return False
    actual = {n['metadata']['name']: n for n in api.get('nodes.longhorn.io', namespace=NAMESPACE)['items']}
    if int(c.get('longhorn_v2_cpu_cores', 0)) and (not actual or any(
            n.get('status', {}).get('cpuPolicy') != 'static' for n in actual.values())):
        return False
    for address, node in nodes.items():
        current = actual.get(node['metadata']['name'])
        if address in clients and (not current or not any(x.get('type') == 'Ready' and x.get('status') == 'True' for x in current.get('status', {}).get('conditions', []))):
            return False
        if not current:
            continue
        disks = current.get('spec', {}).get('disks', {})
        if address in c['longhorn_storage_nodes']:
            matches = [(disk_name, disk) for disk_name, disk in disks.items() if disk.get('path') == c['longhorn_data_path']]
            if len(matches) != 1:
                return False
            disk_name, _ = matches[0]
            state = (current.get('status', {}).get('diskStatus') or {}).get(disk_name) or {}
            if not state.get('diskUUID') or not all(any(x.get('type') == condition and x.get('status') == 'True' for x in state.get('conditions', [])) for condition in ('Ready', 'Schedulable')):
                return False
        elif any(disk.get('diskType', 'filesystem') == 'filesystem' for disk in disks.values()):
            raise ValueError('Longhorn has data disks outside the configured allowlist; evacuate explicitly')
        for disk_name, desired in block_declarations(c, address).items():
            disk = disks.get(disk_name, {})
            state = (current.get('status', {}).get('diskStatus') or {}).get(disk_name) or {}
            if (any(disk.get(key) != desired[key] for key in ('path', 'diskType', 'diskDriver')) or
                    state.get('diskType') != 'block' or state.get('diskDriver') != desired['diskDriver'] or
                    not state.get('diskUUID') or not block_disk_ready(disk, state)):
                return False
            explicit = next(d for d in c['longhorn_block_disks'][address] if d['name'] == disk_name)
            if 'allow_scheduling' in explicit:
                if any(disk.get(field) != desired[field] for field in ('allowScheduling', 'evictionRequested')):
                    return False
                if desired['evictionRequested'] and not block_disk_drained(api, state):
                    return False
    engine = c['longhorn_image_prefix'] + '/longhorn-engine:v' + c['longhorn_version']
    images = api.get('engineimages.longhorn.io', namespace=NAMESPACE)['items']
    if 'v1' in engines(c) and not any(x.get('spec', {}).get('image') == engine and x.get('status', {}).get('state') == 'deployed' and all(x.get('status', {}).get('nodeDeploymentMap', {}).get(n['metadata']['name']) for n in clients.values()) for x in images):
        return False
    managers = api.get('instancemanagers.longhorn.io', namespace=NAMESPACE)['items']
    tags = json.loads((FILES / 'images.json').read_text())[c['longhorn_version']]
    image = c['longhorn_image_prefix'] + '/longhorn-instance-manager:' + tags['longhorn-instance-manager']
    required = [(node, 'v1') for node in clients.values()] if 'v1' in engines(c) else []
    required += [(nodes[address], 'v2') for address in c.get('longhorn_v2_nodes', [])]
    for node, data_engine in required:
        if not any(im.get('spec', {}).get('nodeID') == node['metadata']['name']
                   and not im.get('metadata', {}).get('deletionTimestamp')
                   and im['spec'].get('dataEngine') == data_engine and im['spec'].get('image') == image
                   and im.get('status', {}).get('currentState') == 'running'
                   and any(x.get('type') == 'PodReady' and x.get('status') == 'True'
                           for x in im['status'].get('conditions', []))
                   and (data_engine != 'v2' or v2_manager_ready(c, api, im)) for im in managers):
            return False
    if not api.get('csidriver', 'driver.longhorn.io'):
        return False
    if c['longhorn_snapshot_controller'] == 'managed':
        deployment = api.get('deployment', 'kubeauto-snapshot-controller', 'kube-system')
        if not deployment or deployment.get('status', {}).get('availableReplicas', 0) < 2:
            return False
    return rebuild_qos_ready(c, api)


def v2_cpu_isolation(c, api, manager):
    """Native per-IM override wins; an omitted config preserves native policy."""
    override = manager.get('spec', {}).get('dataEngineSpec', {}).get('v2', {}).get('cpuIsolationEnabled', '')
    if override not in ('', 'true', 'false'):
        raise ValueError('invalid V2 per-instance CPU isolation override')
    if override:
        return override == 'true'
    if 'longhorn_v2_cpu_isolation_enabled' in c:
        return flag(c['longhorn_v2_cpu_isolation_enabled'])
    if c['longhorn_version'] != '1.12.1':
        return False
    setting = api.get('settings.longhorn.io', 'data-engine-cpu-isolation-enabled', NAMESPACE)
    if not setting:
        return False
    try:
        value = json.loads(setting['value'])['v2']
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('invalid native V2 CPU isolation setting') from exc
    if value not in ('true', 'false'):
        raise ValueError('invalid native V2 CPU isolation setting')
    return value == 'true'


def v2_manager_ready(c, api, manager):
    pod = api.get('pod', manager['metadata']['name'], NAMESPACE)
    if not pod or pod.get('metadata', {}).get('deletionTimestamp'):
        return False
    if (pod.get('spec', {}).get('nodeName') != manager['spec']['nodeID'] or not any(
            ref.get('kind') == 'InstanceManager' and ref.get('uid') == manager['metadata']['uid']
            for ref in pod['metadata'].get('ownerReferences', []))):
        raise ValueError('V2 Instance Manager Pod ownership or placement differs')
    containers = pod.get('spec', {}).get('containers', [])
    if len(containers) != 1 or containers[0].get('image') != manager['spec']['image']:
        return False
    resources = containers[0].get('resources', {})
    args = containers[0].get('args', [])
    exclusive = int(c.get('longhorn_v2_cpu_cores', 0))
    expected_args = [('--spdk-cpumask', '' if exclusive else c['longhorn_v2_cpu_mask']),
                     ('--spdk-memory-size', str(c['longhorn_v2_memory_mib']))]
    # CPU Manager automatic allocation is new in 1.12.1; 1.12.0 has no flag.
    if c['longhorn_version'] == '1.12.1':
        expected_args.append(('--spdk-core-number', str(exclusive)))
        for field, argument, default in (
                ('longhorn_v2_iobuf_large_pool_size', '--spdk-iobuf-large-pool-size', 1024),
                ('longhorn_v2_iobuf_small_pool_size', '--spdk-iobuf-small-pool-size', 8192)):
            value = int(c.get(field, default))
            if value > default:
                expected_args.append((argument, str(value)))
            elif argument in args:
                return False
    for key, value in expected_args:
        if args.count(key) != 1 or args.index(key) + 1 >= len(args):
            return False
        actual = args[args.index(key) + 1]
        if key == '--spdk-cpumask' and not exclusive:
            if not re.fullmatch(r'0x[0-9a-fA-F]+', actual) or int(actual, 16) != int(value, 16):
                return False
        elif actual != value:
            return False
    if (('--spdk-no-hugepage' in args) == flag(c['longhorn_v2_hugepages']) or
            ('--spdk-interrupt-mode' in args) != flag(c['longhorn_v2_interrupt_mode'])):
        return False
    isolation = v2_cpu_isolation(c, api, manager)
    if any(args.count(argument) != int(isolation) for argument in (
            '--enable-irq-affinity', '--enable-workqueue-affinity', '--enable-rps')):
        return False
    request = quantity(resources.get('requests', {}).get('cpu', 0))
    if request < (exclusive or int(c['longhorn_v2_cpu_mask'], 16).bit_count()):
        return False
    if exclusive and (pod.get('status', {}).get('qosClass') != 'Guaranteed' or
            request != request.to_integral_value() or
            request != quantity(resources.get('limits', {}).get('cpu', 0)) or
            quantity(resources.get('requests', {}).get('memory', 0)) != 128 * 1024**2 or
            quantity(resources.get('limits', {}).get('memory', 0)) != 128 * 1024**2 or
            manager.get('status', {}).get('dataEngineStatus', {}).get('v2', {}).get('cpuCoreNumber') != exclusive):
        return False
    if flag(c['longhorn_v2_hugepages']):
        if quantity(resources.get('limits', {}).get('hugepages-2Mi', 0)) < int(c['longhorn_v2_memory_mib']) * 1024**2:
            return False
    return True


def empty_for_uninstall(api):
    if any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in api.get('persistentvolumes')['items']):
        raise ValueError('Longhorn uninstall refused: retained or bound PVs remain')
    if api.get('volumes.longhorn.io', namespace=NAMESPACE)['items']:
        raise ValueError('Longhorn uninstall refused: volumes remain')
    for plural, final_states in (('backups', ('Completed', 'Error')), ('systembackups', ('Ready', 'Error')), ('systemrestores', ('Completed', 'Error'))):
        if any(o.get('status', {}).get('state') not in final_states for o in api.get(plural + '.longhorn.io', namespace=NAMESPACE)['items']):
            raise ValueError('Longhorn uninstall refused: unfinished data-protection operation')
    if api.get('crd', 'volumesnapshotcontents.snapshot.storage.k8s.io'):
        if any(o.get('spec', {}).get('driver') == 'driver.longhorn.io' for o in api.get('volumesnapshotcontents.snapshot.storage.k8s.io')['items']):
            raise ValueError('Longhorn uninstall refused: CSI snapshot contents remain')


def bootstrap_node(c, api, address, expected, receipt):
    # Node status heartbeats legitimately change resourceVersion after the
    # longer installation preflight. Retain optimistic concurrency and identity
    # checks; refresh only this metadata update, not the entire installation.
    for attempt in range(3):
        node = api.get('node', expected['metadata']['name'])
        if not node or node['metadata']['uid'] != expected['metadata']['uid']:
            raise ValueError('Longhorn bootstrap node identity changed')
        meta = node['metadata']
        if (meta.get('labels', {}).get('node.longhorn.io/create-default-disk')
                or any(k in meta.get('labels', {}) for k in ('kubeauto.io/longhorn-client',
                    'node.longhorn.io/disable-v2-data-engine'))) and not owned(node):
            raise ValueError('foreign Longhorn disk bootstrap label')
        selected = address in c['longhorn_storage_nodes']
        desired = {'metadata': {'resourceVersion': meta['resourceVersion'], 'labels': {
            'node.longhorn.io/create-default-disk': 'config' if selected else 'false',
            'kubeauto.io/longhorn-client': str(address in c['longhorn_client_nodes']).lower(), **OWNER}}}
        if 'v2' in engines(c):
            desired['metadata']['labels']['node.longhorn.io/disable-v2-data-engine'] = str(address not in c['longhorn_v2_nodes']).lower()
        if selected:
            desired['metadata']['annotations'] = {'node.longhorn.io/default-disks-config': json.dumps([
                {'name': 'kubeauto-data', 'path': c['longhorn_data_path'], 'allowScheduling': True,
                 'storageReserved': int(receipt['total_bytes']) // 10}])}
        if contains(meta, {k: v for k, v in desired['metadata'].items() if k != 'resourceVersion'}):
            return
        try:
            api.patch('node', meta['name'], desired)
            return
        except CommandError as error:
            if not error.stderr.startswith('Error from server (Conflict):'):
                raise
            if attempt == 2:
                raise ValueError('Longhorn node metadata conflict retry limit reached') from None
            print('LONGHORN_NODE_METADATA_RETRY node=' + meta['name'] + ' attempt=' + str(attempt + 1), flush=True)
            time.sleep(0.2 * (attempt + 1))


def release_node_bootstrap(api, values):
    # A failure before Helm can still leave bootstrap labels. Only a receipt
    # bound to this cluster/namespace authorizes their removal.
    if not Path(values).with_name('installation.json').exists():
        return False
    check_installation_identity(api, values, None)
    changed = False
    for expected in api.get('nodes')['items']:
        if not owned(expected):
            continue
        for attempt in range(3):
            node = api.get('node', expected['metadata']['name'])
            if not node or node['metadata']['uid'] != expected['metadata']['uid'] or not owned(node):
                raise ValueError('Longhorn bootstrap cleanup node identity or ownership changed')
            try:
                labels = {'node.longhorn.io/create-default-disk': None, 'kubeauto.io/longhorn-client': None,
                          'kubeauto.io/component': None}
                if 'node.longhorn.io/disable-v2-data-engine' in node['metadata'].get('labels', {}):
                    labels['node.longhorn.io/disable-v2-data-engine'] = None
                api.patch('node', node['metadata']['name'], {'metadata': {
                    'resourceVersion': node['metadata']['resourceVersion'],
                    'labels': labels,
                    'annotations': {'node.longhorn.io/default-disks-config': None}}})
                changed = True
                break
            except CommandError as error:
                if not error.stderr.startswith('Error from server (Conflict):'):
                    raise
                if attempt == 2:
                    raise ValueError('Longhorn bootstrap cleanup conflict retry limit reached') from None
                print('LONGHORN_NODE_CLEANUP_RETRY node=' + node['metadata']['name'], flush=True)
                time.sleep(0.2 * (attempt + 1))
    return changed


RESTORE_REASSERTION = 'kubeauto.io/system-restore-reassertion'


def completed_restore_reassertions(c, api, values):
    """Only this configuration's UID-bound completed restores authorize repair."""
    requests = [r for r in c['longhorn_requests'] if r['kind'] == 'system-restore']
    if not requests:
        return []
    namespace = api.get('namespace', NAMESPACE)
    values_sha = hashlib.sha256(Path(values).read_bytes()).hexdigest()
    result = []
    for request in requests:
        obj = api.get('systemrestores.longhorn.io', request['name'], NAMESPACE)
        if not obj:
            continue
        check_request(obj, request_object(request))
        status = obj.get('status', {})
        if status.get('state') != 'Completed':
            continue
        if (obj['metadata'].get('deletionTimestamp') or not obj['metadata'].get('uid') or
                not namespace or not namespace['metadata'].get('uid') or status.get('error') or
                any(x.get('type') == 'Error' and x.get('status') == 'True' for x in (status.get('conditions') or []))):
            raise ValueError('completed SystemRestore identity or terminal state differs')
        identity = {'restore_uid': obj['metadata']['uid'], 'namespace_uid': namespace['metadata']['uid'],
                    'values_sha256': values_sha}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if obj['metadata'].get('annotations', {}).get(RESTORE_REASSERTION) != fingerprint:
            result.append((request, obj['metadata']['uid'], fingerprint))
    return result


def reassert_completed_restores(c, api, helm, values, nodes):
    """Native restore changes live settings/spec without changing Helm's values.

    Reapply the customer's target profile before its ordinary health wait. The
    same completed request also enters here immediately after its operation.
    A final UID-bound annotation makes replay and interrupted replay idempotent.
    """
    requests = completed_restore_reassertions(c, api, values)
    if not requests:
        return
    def detached():
        return all(v.get('status', {}).get('state') == 'detached' and not v.get('spec', {}).get('nodeID')
                   for v in api.get('volumes.longhorn.io', namespace=NAMESPACE)['items'])
    # Completed means PVC creation has finished, not that the restore engine's
    # temporary attachment has already retired. Wait without changing settings.
    wait('system-restore-all-volumes-detached', int(c['longhorn_operation_timeout']), detached)
    # The finite keys are exactly chart_values' explicitly managed settings;
    # credentials, excluded concurrency settings and unrelated settings stay native.
    settings = []
    for key, value in chart_values(c)['defaultSettings'].items():
        native = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2',
                        re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', key)).lower()
        obj = api.get('settings.longhorn.io', native, NAMESPACE)
        if (not obj or obj.get('kind') != 'Setting' or obj.get('apiVersion') != 'longhorn.io/v1beta2' or
                obj['metadata'].get('name') != native or not obj['metadata'].get('uid') or
                not obj['metadata'].get('resourceVersion')):
            raise ValueError('managed restore setting identity missing: ' + native)
        desired = (json.dumps(value, sort_keys=True, separators=(',', ':')) if isinstance(value, (dict, list)) else
                   str(value).lower() if isinstance(value, bool) else str(value))
        if isinstance(value, (dict, list)):
            try: same = json.loads(obj.get('value', '')) == value
            except (ValueError, TypeError): same = False
        else:
            same = obj.get('value') == desired
        settings.append((obj, desired, same))
    for obj, value, same in settings:
        if not same:
            api.patch('settings.longhorn.io', obj['metadata']['name'], {'metadata': {
                'uid': obj['metadata']['uid'], 'resourceVersion': obj['metadata']['resourceVersion']},
                'value': value}, NAMESPACE)
    if not detached():
        raise ValueError('system restore reassertion requires all volumes detached')
    timeout = int(c['longhorn_ready_timeout'])
    chart = FILES / ('longhorn-' + c['longhorn_version'] + '.tgz')
    print('LONGHORN_STAGE_BEGIN action=system-restore-reassert', flush=True)
    run([helm, 'upgrade', '--install', 'longhorn', chart, '-n', NAMESPACE, '-f', values,
         '--post-renderer', sys.executable, '--post-renderer-args=' + str(Path(__file__).resolve()),
         '--post-renderer-args=post-render', '--wait', '--timeout', str(timeout) + 's', '--history-max', '10'], timeout=timeout + 60)
    def ready():
        check_block_identity(c, api, nodes, values)
        return reconcile_filesystem_disks(c, api, nodes) and reconcile_block_disks(c, api, nodes) and healthy(c, api, nodes)
    wait('system-restore-target-profile-ready', max(timeout, int(c['longhorn_operation_timeout'])), ready)
    for request, uid, fingerprint in requests:
        current = api.get('systemrestores.longhorn.io', request['name'], NAMESPACE)
        check_request(current, request_object(request))
        if (not current or current['metadata']['uid'] != uid or current.get('status', {}).get('state') != 'Completed' or
                current['metadata'].get('deletionTimestamp') or current.get('status', {}).get('error')):
            raise ValueError('SystemRestore identity changed before reassertion completion')
        api.patch('systemrestores.longhorn.io', request['name'], {'metadata': {'uid': uid,
            'resourceVersion': current['metadata']['resourceVersion'], 'annotations': {RESTORE_REASSERTION: fingerprint}}}, NAMESPACE)
    print('LONGHORN_SYSTEM_RESTORE_REASSERT_PASS requests=' + str(len(requests)), flush=True)


def reconcile(c, api, helm, values, nodes, release):
    if c['longhorn_action'] == 'retire-block-disks':
        retirement = block_retirement()
        retirement.reconcile(c, api, values, sys.modules[__name__])
        retirement.atomic(Path(values).with_name('block-retirement-release.json'),
            retirement.release_proofs(c, api, values, sys.modules[__name__]))
        return
    timeout = int(c['longhorn_ready_timeout'])
    if c['longhorn_action'] == 'uninstall':
        if not release:
            # No false success when a previous uninstall left its CRDs behind.
            guard(api)
            changed = release_node_bootstrap(api, values)
            retire_recurring_receipt(values)
            retire_block_identity(values)
            print('LONGHORN_UNINSTALL_PASS changed=' + str(int(changed))); return
        empty_for_uninstall(api)
        api.patch('settings.longhorn.io', 'deleting-confirmation-flag', {'value': 'true'}, NAMESPACE)
        run([helm, 'uninstall', 'longhorn', '-n', NAMESPACE, '--wait', '--timeout', str(timeout) + 's'], timeout=timeout + 60)
        for sc in api.get('storageclasses')['items']:
            if owned(sc) and sc.get('provisioner') == 'driver.longhorn.io':
                run(api.prefix + ['delete', 'storageclass', sc['metadata']['name'], '--wait=true', '--timeout=120s'])
        if api.get('crd', 'volumesnapshotclasses.snapshot.storage.k8s.io'):
            current = api.get('volumesnapshotclass', 'longhorn-snapshot')
            require_owned(current)
            if current:
                api.delete(current)
        current = api.get('networkpolicy', 'kubeauto-longhorn-metrics', NAMESPACE)
        require_owned(current)
        if current:
            api.delete(current)
        guard(api)
        release_node_bootstrap(api, values)
        retire_recurring_receipt(values)
        retire_block_identity(values)
        print('LONGHORN_UNINSTALL_PASS changed=1'); return
    snapshots = snapshot_preflight(c, api)
    receipts = c.get('longhorn_disk_receipts', {})
    if set(receipts) != set(c['longhorn_storage_nodes']) or any(r.get('path') != c['longhorn_data_path'] or not r.get('uuid') for r in receipts.values()):
        raise ValueError('missing current independent host filesystem receipts')
    context = host_context(c, api)
    if any(r.get('cluster_uid') != context['cluster_uid'] or r.get('node_uid') != context['node_uids'][address]
           for address, r in receipts.items()):
        raise ValueError('host filesystem receipt belongs to another or unknown cluster/node')
    check_block_receipts(c, context)
    check_block_identity(c, api, nodes, values)
    reassert_completed_restores(c, api, helm, values, nodes)
    # All read-only ownership checks, including the shared CSI boundary, finish first.
    if not api.get('namespace', NAMESPACE):
        api.apply(resource('Namespace', NAMESPACE, api='v1', namespace=''), create=True)
    record_installation(api, values)
    for obj in snapshots:
        api.apply(obj)
    if snapshots:
        for plural in SNAPSHOT_CRDS:
            wait('snapshot-crd-' + plural, 120, lambda plural=plural: any(x.get('type') == 'Established' and x.get('status') == 'True' for x in api.get('crd', plural + '.snapshot.storage.k8s.io').get('status', {}).get('conditions', [])))
    for address, node in nodes.items():
        bootstrap_node(c, api, address, node, receipts.get(address))
    values_obj = json.loads(Path(values).read_text())
    previous = json.loads(run([helm, 'get', 'values', 'longhorn', '-n', NAMESPACE, '-o', 'json'])) if release else None
    changed = not release or previous != values_obj or release.get('status') != 'deployed'
    if changed:
        chart = FILES / ('longhorn-' + c['longhorn_version'] + '.tgz')
        print('LONGHORN_STAGE_BEGIN action=helm-reconcile', flush=True)
        run([helm, 'upgrade', '--install', 'longhorn', chart, '-n', NAMESPACE, '-f', values,
             '--post-renderer', sys.executable, '--post-renderer-args=' + str(Path(__file__).resolve()),
             '--post-renderer-args=post-render', '--wait', '--timeout', str(timeout) + 's', '--history-max', '10'], timeout=timeout + 60)
    def control_ready():
        check_block_identity(c, api, nodes, values)
        return reconcile_filesystem_disks(c, api, nodes) and reconcile_block_disks(c, api, nodes) and healthy(c, api, nodes)
    evicting = any(flag(d.get('eviction_requested', False))
                   for disks in c.get('longhorn_block_disks', {}).values() for d in disks)
    wait('control-and-disks-ready', max(timeout, int(c['longhorn_operation_timeout'])) if evicting else timeout, control_ready)
    check_block_identity(c, api, nodes, values, record=True)
    for sc in c['longhorn_storage_classes']:
        api.apply(storage_class(sc))
    if c['longhorn_snapshot_controller'] != 'disabled':
        api.apply(snapshot_class())
    if c['longhorn_backup_target']:
        api.patch('backuptargets.longhorn.io', 'default', {'spec': {'backupTargetURL': c['longhorn_backup_target'],
                  'credentialSecret': c['longhorn_backup_secret'], 'pollInterval': '5m0s'}}, NAMESPACE)
        wait('backup-target-ready', timeout, lambda: api.get('backuptargets.longhorn.io', 'default', NAMESPACE).get('status', {}).get('available'))
    reconcile_jobs(c, api, values)
    if flag(c['longhorn_monitoring_enabled']):
        api.apply(metrics_policy(c))
    else:
        current = api.get('networkpolicy', 'kubeauto-longhorn-metrics', NAMESPACE)
        require_owned(current)
        if current:
            api.delete(current)
    for request in c['longhorn_requests']:
        operation(c, api, request)
        if request['kind'] == 'system-restore':
            reassert_completed_restores(c, api, helm, values, nodes)
    print('LONGHORN_RECONCILE_PASS changed=' + str(int(changed)), flush=True)


def recurring_receipt(values):
    installation = json.loads(Path(values).with_name('installation.json').read_text())
    path = Path(values).with_name('recurring-jobs.json')
    if path.is_symlink():
        raise ValueError('symlink recurring job receipt')
    identity = {key: installation[key] for key in ('cluster_uid', 'namespace_uid')}
    row = json.loads(path.read_text()) if path.exists() else dict(identity, jobs={})
    if any(row.get(key) != value for key, value in identity.items()):
        raise ValueError('recurring job receipt belongs to another installation')
    return path, row


def save_recurring_receipt(path, row):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        json.dump(row, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.chmod(0o600); temporary.replace(path)


def retire_recurring_receipt(values):
    path = Path(values).with_name('recurring-jobs.json')
    if not path.exists() and not path.is_symlink():
        return
    _, row = recurring_receipt(values)
    archive = path.with_name('recurring-jobs-uninstalled-' + row['namespace_uid'] + '.json')
    if archive.exists() or archive.is_symlink():
        raise ValueError('recurring job uninstall archive already exists')
    path.rename(archive)


def check_recurring_identity(c, api, values):
    # Before the initial installation no RecurringJob API or local receipt exists.
    if not api.get('crd', 'recurringjobs.longhorn.io'):
        return
    _, row = recurring_receipt(values)
    for job in c['longhorn_recurring_jobs']:
        current = api.get('recurringjobs.longhorn.io', job['name'], NAMESPACE)
        if current and row['jobs'].get(job['name']) != current['metadata']['uid']:
            raise ValueError('recurring job has no matching installation UID receipt')
    for object_name, uid in row['jobs'].items():
        current = api.get('recurringjobs.longhorn.io', object_name, NAMESPACE)
        require_owned(current, LABELS)
        if current and current['metadata']['uid'] != uid:
            raise ValueError('managed recurring job was replaced')


def reconcile_jobs(c, api, values):
    path, row = recurring_receipt(values)
    for job in c['longhorn_recurring_jobs']:
        current = api.get('recurringjobs.longhorn.io', job['name'], NAMESPACE)
        require_owned(current, LABELS)
        if current and row['jobs'].get(job['name']) != current['metadata']['uid']:
            raise ValueError('recurring job has no matching installation UID receipt')
        obj = resource('RecurringJob', job['name'], {k: v for k, v in job.items() if k != 'name'})
        if current:
            # Keep native defaults/labels and metadata/finalizers. PUT requires
            # the exact UID/resourceVersion and avoids create-vs-SSA ownership.
            spec = dict(current['spec'], **obj['spec'])
            spec['parameters'] = job.get('parameters', {})
            obj = {key: current[key] for key in ('apiVersion', 'kind', 'metadata')}
            obj['spec'] = spec
            result = api.replace(obj) if spec != current['spec'] else current
        else:
            result = api.apply(obj, create=True)
        if current and result['metadata']['uid'] != current['metadata']['uid']:
            raise ValueError('recurring job identity changed during update')
        row['jobs'][job['name']] = result['metadata']['uid']
        save_recurring_receipt(path, row)
    desired = {job['name'] for job in c['longhorn_recurring_jobs']}
    for object_name, uid in list(row['jobs'].items()):
        if object_name in desired:
            continue
        current = api.get('recurringjobs.longhorn.io', object_name, NAMESPACE)
        require_owned(current, LABELS)
        if current and current['metadata']['uid'] != uid:
            raise ValueError('managed recurring job was replaced')
        # Match datastore.DeleteRecurringJob: foreground GC keeps the CR in
        # deletion long enough for its controller to remove selector labels.
        if current:
            api.delete(current, propagation_policy='Foreground')
        def stopped():
            replacement = api.get('recurringjobs.longhorn.io', object_name, NAMESPACE)
            if replacement and replacement['metadata']['uid'] != uid:
                raise ValueError('recurring job identity changed during removal')
            return not replacement and not any(
                any(ref.get('uid') == uid for ref in obj['metadata'].get('ownerReferences', []))
                for obj in api.get('cronjobs', namespace=NAMESPACE)['items'])
        wait('recurring-job-removed-' + object_name, int(c['longhorn_ready_timeout']), stopped)
        del row['jobs'][object_name]
        save_recurring_receipt(path, row)


def post_render():
    objects = [o for o in yaml.safe_load_all(sys.stdin.read()) if o]
    for obj in objects:
        obj.setdefault('metadata', {}).setdefault('labels', {}).update(OWNER)
    yaml.safe_dump_all(objects, sys.stdout, sort_keys=False)


def operation(c, api, request):
    obj = request_object(request)
    kind, object_name = obj['kind'] + '.longhorn.io', obj['metadata']['name']
    current = api.get(kind, object_name, NAMESPACE)
    if current:
        check_request(current, obj)
    else:
        if request['kind'] == 'snapshot':
            v = api.get('volumes.longhorn.io', request['volume'], NAMESPACE)
            if not v or v.get('status', {}).get('state') != 'attached' or v.get('status', {}).get('robustness') != 'healthy':
                raise ValueError('snapshot requires an attached healthy source volume')
        elif request['kind'] == 'backup':
            snapshot = api.get('snapshots.longhorn.io', request['snapshot'], NAMESPACE)
            target = api.get('backuptargets.longhorn.io', 'default', NAMESPACE)
            if not snapshot or not snapshot.get('status', {}).get('readyToUse') or not target or not target.get('status', {}).get('available'):
                raise ValueError('backup requires a ready snapshot and available backup target')
            obj['metadata']['labels'].update({'backup-target': 'default', 'backup-volume': snapshot['spec']['volume']})
        elif request['kind'] == 'system-restore':
            if any(v.get('status', {}).get('state') != 'detached' for v in api.get('volumes.longhorn.io', namespace=NAMESPACE)['items']):
                raise ValueError('system restore requires all volumes detached')
        current = api.apply(obj, create=True)
    uid = current['metadata']['uid']
    def complete():
        state = api.get(kind, object_name, NAMESPACE)
        if not state or state['metadata']['uid'] != uid:
            raise ValueError('Longhorn request identity changed while waiting')
        status = state.get('status', {})
        if status.get('error') or status.get('state') in ('Error', 'Unknown') or any(x.get('type') == 'Error' and x.get('status') == 'True' for x in (status.get('conditions') or [])):
            raise ValueError('Longhorn request reached an error state')
        expected = {'snapshot': None, 'backup': 'Completed', 'system-backup': 'Ready', 'system-restore': 'Completed'}[request['kind']]
        return bool(status.get('readyToUse')) if expected is None else status.get('state') == expected and (request['kind'] != 'backup' or bool(status.get('url')))
    wait('request-' + object_name, int(c['longhorn_operation_timeout']), complete)
    print('LONGHORN_REQUEST_PASS kind=' + request['kind'] + ' name=' + object_name, flush=True)


def guard(api, node_ip=''):
    """Fail closed on API errors. Read all namespaces, including foreign Longhorn."""
    crds = {x['metadata']['name'] for x in api.get('crds')['items']}
    pvs = [p for p in api.get('persistentvolumes')['items'] if p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io']
    if not node_ip:
        if pvs:
            raise ValueError('Longhorn PVs remain; migrate/retain data and uninstall storage before destroy')
        if any(x.endswith('.longhorn.io') for x in crds):
            raise ValueError('Longhorn CRDs remain; finish native storage uninstall before destroy')
    elif pvs or any(x.endswith('.longhorn.io') for x in crds):
        matches = [n for n in api.get('nodes')['items'] if any(a.get('type') == 'InternalIP' and a.get('address') == node_ip for a in n.get('status', {}).get('addresses', []))]
        if len(matches) != 1:
            raise ValueError('cannot identify the node for Longhorn removal protection')
        node_name = matches[0]['metadata']['name']
        for node in (api.get('nodes.longhorn.io', namespace='*')['items'] if 'nodes.longhorn.io' in crds else []):
            if node['metadata']['name'] == node_name:
                if node.get('spec', {}).get('allowScheduling', True) or node.get('spec', {}).get('disks'):
                    raise ValueError('Longhorn node must be unschedulable with all disks evacuated and removed')
        for plural, field in (('replicas', 'nodeID'), ('engines', 'nodeID')):
            if plural + '.longhorn.io' in crds:
                if any(x.get('spec', {}).get(field) == node_name for x in api.get(plural + '.longhorn.io', namespace='*')['items']):
                    raise ValueError('Longhorn data processes or replicas remain on the node')
        if any(a.get('spec', {}).get('attacher') == 'driver.longhorn.io' and
               a.get('spec', {}).get('nodeName') == node_name for a in api.get('volumeattachments.storage.k8s.io')['items']):
            raise ValueError('Longhorn CSI attachment remains on the node')
        for plural in ('volumes', 'volumeattachments', 'instancemanagers', 'backingimagemanagers', 'backingimagedatasources'):
            if plural + '.longhorn.io' not in crds:
                continue
            for obj in api.get(plural + '.longhorn.io', namespace='*')['items']:
                spec, status = obj.get('spec', {}), obj.get('status', {})
                if plural == 'volumes' and (spec.get('nodeID') == node_name or status.get('currentNodeID') == node_name or status.get('pendingNodeID') == node_name):
                    raise ValueError('Longhorn volume remains attached to the node')
                if plural == 'volumeattachments' and any(t and t.get('nodeID') == node_name for t in (spec.get('attachmentTickets') or {}).values()):
                    raise ValueError('Longhorn attachment ticket remains on the node')
                if spec.get('nodeID') != node_name:
                    continue
                if plural == 'instancemanagers':
                    if any(status.get(k) for k in ('instanceEngines', 'instanceEngineFrontends', 'instanceReplicas', 'instanceShards', 'instanceShardGroups', 'backingImages')):
                        raise ValueError('Longhorn instance manager still contains instances')
                    if status.get('currentState') not in ('running', 'stopped'):
                        raise ValueError('Longhorn instance manager state is unverified')
                elif plural in ('backingimagemanagers', 'backingimagedatasources'):
                    raise ValueError('Longhorn backing image process remains on the node')
    print('LONGHORN_REMOVAL_GUARD_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('validate', 'preflight', 'reconcile', 'post-render', 'guard', 'retire-finalize'))
    parser.add_argument('--config'); parser.add_argument('--kubeconfig'); parser.add_argument('--helm', default='helm')
    parser.add_argument('--node-ip', default='')
    args = parser.parse_args()
    if args.action == 'post-render':
        post_render(); return
    if args.action == 'guard':
        guard(API(args.kubeconfig), args.node_ip); return
    c = json.loads(Path(args.config).read_text())
    validate(c)
    if not flag(c['longhorn_install']):
        print('LONGHORN_DISABLED_PASS'); return
    if args.action == 'validate':
        print('LONGHORN_CONFIG_PASS'); return
    # Set KUBECONFIG only in this process, never change the caller's context.
    os.environ['KUBECONFIG'] = args.kubeconfig
    api = API(args.kubeconfig)
    values_path = Path(args.config).with_name('values.json')
    values_path.write_text(json.dumps(chart_values(c), sort_keys=True)); values_path.chmod(0o600)
    if args.action == 'retire-finalize':
        if c['longhorn_action'] != 'retire-block-disks':
            raise ValueError('retire-finalize requires explicit block retirement action')
        block_retirement().finalize(c, api, values_path, sys.modules[__name__]); return
    nodes, release = preflight(c, api, args.helm, values_path)
    if args.action == 'preflight':
        context_path = Path(args.config).with_name('host-context.json')
        context_path.write_text(json.dumps(host_context(c, api), sort_keys=True)); context_path.chmod(0o600)
        print('LONGHORN_PREFLIGHT_PASS'); return
    reconcile(c, api, args.helm, values_path, nodes, release)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, TimeoutError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print('LONGHORN_PRODUCT_FAIL reason=' + str(error), file=sys.stderr)
        sys.exit(1)
