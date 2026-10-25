#!/usr/bin/env python3
"""Owned one-node V2 AIO prerequisite for independent disaster recovery.

Uses config + kubecli for installation. Does not qualify site HA or DR business.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

import yaml

BASE = Path('/usr/local/kubeauto')


def full_material():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('longhorn_full_material', Path(__file__).with_name('longhorn-full-material.py'))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def evidence_path(historical, *, optional=False):
    return full_material().path(BASE, historical, optional=optional)


def load(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location('dr_install_'+name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    target = load('longhorn-v2-dr-target.py')
    lab = target.fixture()
    lab.verify_core_smoke = lambda: core_current(lab)
    lab.restore_hosts = lambda: restore_hosts(lab)
    def step(pid, name, *, expected_refusal=None):
        if expected_refusal is not None:
            raise ValueError('target initial installation does not own refusal scenarios')
        target.supervise(lab, pid, name)
    lab.supervise_core = step
    return target, lab


def core_current(lab):
    lab.core_identity()
    evidence = json.loads((evidence_path('tests/evidence/longhorn-v2-dr-target-core-20261020.json')).read_text())
    state = lab.owner()
    if (evidence['status'] != 'pass' or evidence['rc'] or evidence['failure_markers'] or
            any(state.get(key) != value for key, value in evidence['baseline_identity'].items()) or
            state.get('target_core_smoke_sha256') != hashlib.sha256((BASE/'tests/helpers/kubernetes-production-smoke.sh').read_bytes()).hexdigest()):
        raise ValueError('target installation requires its current independent core and same-node smoke')
    for name in ('tests/helpers/longhorn-v2-dr-target.py', 'tests/helpers/kubernetes-production-smoke.sh',
                 'tests/helpers/longhorn-lab.py', 'roles/longhorn/files/longhorn.py'):
        if not load('longhorn-v2-install.py').qualification_input_current(evidence, name):
            raise ValueError('target core dependency changed: '+name)
    print('LONGHORN_V2_DR_TARGET_CORE_CURRENT nodes=1 cross_node=false', flush=True)


def host_inputs(target, lab):
    # An installed, runner-owned core is expected here. The initial empty-host
    # probe must not be relaxed globally or reused after Longhorn owns the disk.
    core_current(lab); state = lab.owner()
    if state.get('namespace_uid') or state.get('host_state') or lab.get('namespace', 'longhorn-system'):
        raise ValueError('target host qualification requires its owned core before storage')
    pool = load('longhorn-v2-pool.py')
    host = json.loads(lab.remote(target.NODE, ['python3', '-c', pool.PROBE]).stdout)
    assessment = pool.assess(host)
    if (assessment['blockers'] != ['existing-host-state-requires-ownership-check'] or
            host['machine_id'] != state['raw_disks'][target.NODE]['machine_id'] or
            host['hostname'] != 'longhorn-node-149' or host['ceph_files'] or host['hugepages_2mi'] != '0' or
            host['services']['docker']['rc'] == 0 or host['services']['ceph.target']['rc'] == 0):
        raise ValueError('target host contains foreign or unexpected state')
    clean_disk = dict(assessment, blockers=[])
    expected = state['raw_disks'][target.NODE]
    if target.disk_identity(clean_disk) != (expected['identity'], expected['size_bytes'], expected['path']):
        raise ValueError('target unused raw disk differs from the core admission')
    print('LONGHORN_V2_DR_TARGET_OWNED_CORE_INPUTS_VERIFIED nodes=1 raw_disk=unused', flush=True)


def resource_config():
    return {'longhorn_v2_cpu_mask': '0x1', 'longhorn_v2_cpu_cores': 0,
            'longhorn_v2_cpu_reservation_percentage': 25, 'longhorn_v2_memory_mib': 2048,
            'longhorn_v2_hugepages': True}


def verify_resources(lab):
    lab.core_identity()
    resources = load('longhorn-v2-resources.py')
    record = resources.read_record(lab)
    for node, row in record['hosts'].items():
        if row['phase'] != 'ready':
            raise ValueError('target page pool is not ready')
        current = resources.probe(lab, node)
        if any(current[key] != row['current'][key] for key in ('machine_id', 'boot_id', 'total')):
            raise ValueError('target prepared page pool identity changed')
        resources.product().check_v2_resources(resource_config(), lab.get('node', 'longhorn-node-149'))
    print('LONGHORN_V2_DR_TARGET_RESOURCE_ADMISSION_PASS nodes=1 pages=1024 mask=0x1 reservation_percent=25', flush=True)


def restore_resources(lab):
    resources = load('longhorn-v2-resources.py')
    resources.no_storage(lab)
    path = resources.record_path(lab)
    if not path.exists() and not path.is_symlink():
        print('LONGHORN_V2_DR_TARGET_RESOURCES_CLEAN_PASS state=unchanged', flush=True)
        return
    record = resources.read_record(lab)
    for node, row in record['hosts'].items():
        if row['phase'] in ('write-intent', 'restore-intent'):
            raise ValueError('ambiguous target page write; retain journal')
        current = resources.probe(lab, node)
        resources.validate_host(lab.owner(), node, current)
        if any(current[key] != row['current'][key] for key in
               ('machine_id', 'boot_id', 'total', 'free', 'reserved', 'kubelet_active')):
            raise ValueError('target page pool changed outside its journal')
    for node, row in record['hosts'].items():
        if row['phase'] != 'restored':
            if row['current']['total'] != row['original']['total']:
                row['phase'] = 'restore-intent'; lab.write_private(path, record)
                row['current'] = resources.probe(lab, node, target=row['original']['total'], expected=row['current'])
                row['phase'] = 'restore-confirmed'; lab.write_private(path, record)
                if row['current']['total'] != row['original']['total']:
                    raise ValueError('target original page pool was not restored')
            resources.refresh_kubelet(lab, node, row['original']['total'])
            row['phase'] = 'restored'; lab.write_private(path, record)
    archive = path.with_name('longhorn-v2-resources-restored-'+record['id']+'.json')
    if archive.exists() or archive.is_symlink():
        raise ValueError('target resource archive already exists')
    path.rename(archive)
    print('LONGHORN_V2_DR_TARGET_RESOURCES_CLEAN_PASS nodes=1 pages=original', flush=True)


def capture_hosts(lab):
    lab.core_identity(); state = lab.owner()
    if state.get('host_state'):
        raise ValueError('target host state is already journaled')
    install = load('longhorn-v2-install.py')
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in install.host_files(lab).items()}
    hosts = {}
    for node in lab.NODES:
        host = json.loads(lab.remote(node, ['python3', '-c', lab.host_state_script()],
            stdin=json.dumps({'action': 'capture', 'expected': expected})).stdout)
        original = install.multipath(lab, node, 'probe')
        if original['machine_id'] != state['raw_disks'][node]['machine_id']:
            raise ValueError('target multipath machine changed')
        host['multipath'] = {'original': original, 'current': original, 'phase': 'planned'}
        hosts[node] = host
    state['host_state'] = {'expected': expected, 'nodes': hosts}; lab.write_private(lab.STATE, state)
    print('LONGHORN_V2_DR_TARGET_HOST_JOURNALED nodes=1 packages=retained modules=retained', flush=True)


def quiesce_multipath(lab):
    lab.core_identity(); state = lab.owner(); rows = state['host_state']['nodes']
    if set(rows) != set(lab.NODES) or lab.get('namespace', 'longhorn-system'):
        raise ValueError('target multipath requires its original pre-install journal')
    install = load('longhorn-v2-install.py')
    for node, row in rows.items():
        entry = row['multipath']
        if entry['phase'] != 'planned':
            raise ValueError('target cannot adopt a previous multipath write')
        install.multipath(lab, node, 'check', expected=entry['current'])
    for node, row in rows.items():
        entry = row['multipath']; entry['phase'] = 'stop-intent'; lab.write_private(lab.STATE, state)
        entry['current'] = install.multipath(lab, node, 'stop', expected=entry['current'])
        entry['phase'] = 'stopped'; lab.write_private(lab.STATE, state)
    print('LONGHORN_V2_DR_TARGET_MULTIPATH_QUIESCED nodes=1 maps=0 enable_state=unchanged', flush=True)


def restore_hosts(lab):
    lab.core_identity(); state = lab.owner(); journal = state.get('host_state')
    if not journal or set(journal['nodes']) != set(lab.NODES):
        raise ValueError('target restoration requires its complete host journal')
    lab.execute([sys.executable, BASE/'roles/longhorn/files/longhorn.py', 'guard',
                 '--kubeconfig', lab.DIRECTORY/'kubectl.kubeconfig'])
    install = load('longhorn-v2-install.py')
    installed = {name: hashlib.sha256(data).hexdigest() for name, data in install.host_files(lab).items()}
    for node, before in journal['nodes'].items():
        entry = before['multipath']
        if entry['phase'] not in ('planned', 'stopped', 'restored'):
            raise ValueError('ambiguous target multipath write; retain journal')
        install.multipath(lab, node, 'check', expected=entry['current'])
    for action in ('check', 'restore'):
        for node, before in journal['nodes'].items():
            lab.remote(node, ['python3', '-c', lab.host_state_script()], stdin=json.dumps({
                'action': action, 'expected': journal['expected'], 'installed': installed, 'before': before}))
    for node, before in journal['nodes'].items():
        entry = before['multipath']; entry['phase'] = 'restore-intent'; lab.write_private(lab.STATE, state)
        entry['current'] = install.multipath(lab, node, 'restore', expected=entry['current'], original=entry['original'])
        entry['phase'] = 'restored'; lab.write_private(lab.STATE, state)
    state['host_state'] = None; lab.write_private(lab.STATE, state)
    print('LONGHORN_V2_DR_TARGET_HOST_RESTORED nodes=1 packages=retained modules=retained', flush=True)


def configure(lab):
    core_current(lab); verify_resources(lab); state = lab.owner()
    if state.get('namespace_uid') or lab.get('namespace', 'longhorn-system'):
        raise ValueError('target initial installation requires no previous namespace')
    path = lab.DIRECTORY/'config.yml'
    if path.is_symlink():
        raise ValueError('symlink target installation configuration')
    config = yaml.safe_load(path.read_text())
    if config['longhorn_version'] != '1.12.1' or config['longhorn_install'] != 'no':
        raise ValueError('target requires the pinned disabled initial configuration')
    disks = {node: [{'name': 'kubeauto-block', 'path': row['path'], 'identity': row['identity'],
                    'disk_driver': 'aio', 'storage_reserved': 1024**3}]
             for node, row in state['raw_disks'].items()}
    if set(disks) != set(lab.NODES) or len(lab.NODES) != 1:
        raise ValueError('target storage profile is strictly single-node')
    config.update(resource_config(), longhorn_data_engines=['v2'], longhorn_allow_engine_coexistence=False,
        longhorn_storage_nodes=[], longhorn_v2_nodes=list(lab.NODES), longhorn_block_disks=disks,
        longhorn_default_replica_count=1)
    for storage_class in config['longhorn_storage_classes']:
        storage_class.update(data_engine='v2', replicas=1)
    path.write_text(yaml.safe_dump(config, sort_keys=False)); lab.prepare_credentials()
    print('LONGHORN_V2_DR_TARGET_AIO_CONFIGURED nodes=1 raw_disks=1 replicas=1', flush=True)


def health(lab):
    lab.installation_health()
    for setting, expected in (('v1-data-engine', 'false'), ('v2-data-engine', 'true')):
        if lab.get('settings.longhorn.io', setting, 'longhorn-system').get('value') != expected:
            raise ValueError('target engine setting did not converge')
    managers = lab.get('instancemanagers.longhorn.io', namespace='longhorn-system')['items']
    if len(managers) != 1 or managers[0]['spec'].get('dataEngine') != 'v2':
        raise ValueError('target instance-manager topology differs')
    receipt = lab.DIRECTORY/'yml/longhorn/block-disks.json'
    if receipt.is_symlink():
        raise ValueError('symlink target block receipt')
    rows = json.loads(receipt.read_text())['nodes']
    if set(rows) != set(lab.NODES):
        raise ValueError('target block receipt topology differs')
    for node, row in rows.items():
        native = lab.get('nodes.longhorn.io', row['name'], 'longhorn-system')
        if native['metadata']['uid'] != row['longhorn_node_uid']:
            raise ValueError('target native node identity changed')
        for name, disk in row['disks'].items():
            spec = native['spec']['disks'][name]; actual = native['status']['diskStatus'][name]
            if (actual['diskUUID'] != disk['disk_uuid'] or spec.get('diskType') != 'block' or
                    spec.get('diskDriver') != 'aio' or spec.get('path') != lab.owner()['raw_disks'][node]['path']):
                raise ValueError('target native raw disk identity changed')
    print('LONGHORN_V2_DR_TARGET_AIO_HEALTHY nodes=1 instance_managers=1 site_ha=false', flush=True)


def replica_ready(lab, journal):
    volume = lab.get('volumes.longhorn.io', journal['volume'], 'longhorn-system')
    if not volume or volume['metadata']['uid'] != journal['volume_uid']:
        raise ValueError('target business volume changed')
    if volume['spec'].get('dataEngine') != 'v2' or volume['spec'].get('numberOfReplicas') != 1:
        raise ValueError('target business did not select single-replica V2')
    if volume['status'].get('robustness') != 'healthy':
        return False
    replicas = [r for r in lab.get('replicas.longhorn.io', namespace='longhorn-system')['items']
                if r['spec']['volumeName'] == journal['volume']]
    if len(replicas) != 1:
        raise ValueError('target business replica topology differs')
    replica = replicas[0]; spec = replica['spec']
    native = lab.get('nodes.longhorn.io', 'longhorn-node-149', 'longhorn-system')
    disk_uuid = native['status']['diskStatus']['kubeauto-block']['diskUUID']
    if spec.get('dataEngine') != 'v2' or spec.get('nodeID') != 'longhorn-node-149' or spec.get('diskID') != disk_uuid:
        raise ValueError('target replica is outside its admitted raw disk')
    return (replica['status'].get('currentState') == 'running' and bool(spec.get('healthyAt')) and not spec.get('failedAt'))


def business(lab, pid):
    health(lab); state = lab.owner(); namespace = 'longhorn-business-dr-'+uuid.uuid4().hex[:16]
    record = lab.DIRECTORY/'longhorn-install-business.json'
    if record.exists() or record.is_symlink() or lab.get('namespace', namespace):
        raise ValueError('target business requires no previous fixture')
    sc = lab.get('storageclass', 'longhorn')
    if (sc.get('provisioner') != 'driver.longhorn.io' or sc.get('reclaimPolicy') != 'Retain' or
            sc.get('parameters', {}).get('dataEngine') != 'v2' or sc.get('parameters', {}).get('numberOfReplicas') != '1'):
        raise ValueError('target product StorageClass differs from single-replica V2')
    ns = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps({
        'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}})).stdout)
    journal = {'namespace': namespace, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
               'storage_class': 'longhorn', 'reclaim_policy': 'Retain'}
    lab.write_private(record, journal)
    with lab.scoped_cleanup(lambda: lab.cleanup_business(record, relay_pid=pid)):
        journal['defaults'] = lab.wait('dr-target-business-defaults', 60, lambda: lab.namespace_defaults(namespace))
        lab.write_private(record, journal)
        def create(obj):
            return lab.create_business_object(journal, record, obj)
        claim = create({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
            'metadata': {'name': 'data', 'namespace': namespace}, 'spec': {'accessModes': ['ReadWriteOnce'],
            'storageClassName': 'longhorn', 'resources': {'requests': {'storage': '2Gi'}}}})
        def bound():
            pvc = lab.get('pvc', 'data', namespace)
            return pvc and pvc.get('status', {}).get('phase') == 'Bound'
        lab.wait('dr-target-pvc-bound', 600, bound)
        lab.bind_business_volume(journal, record)
        if not journal.get('volume_uid'):
            raise ValueError('target business native volume was not bound')
        def ready(name):
            pod = lab.get('pod', name, namespace)
            if not pod or pod['metadata']['uid'] != journal['objects']['Pod/'+name]:
                raise ValueError('target client identity changed')
            return any(c['type'] == 'Ready' and c['status'] == 'True' for c in pod.get('status', {}).get('conditions', []))
        create(lab.client_pod(namespace, 'writer', 'data', 'longhorn-node-149'))
        lab.wait('dr-target-writer-ready', 600, lambda: ready('writer'))
        payload = state['run_id']; digest = hashlib.sha256(payload.encode()*262144).hexdigest()
        actual = lab.kubectl('-n', namespace, 'exec', 'writer', '--', 'python3', '-c', lab.payload_program(payload)).stdout.strip()
        if actual != digest:
            raise ValueError('target fsync digest differs')
        lab.wait('dr-target-one-raw-replica', 600, lambda: replica_ready(lab, journal))
        writer = lab.get('pod', 'writer', namespace)
        if not writer or writer['metadata']['uid'] != journal['objects']['Pod/writer']:
            raise ValueError('target writer was replaced before remount')
        lab.remove(writer)
        lab.wait('dr-target-writer-deleted', 180, lambda: lab.get('pod', 'writer', namespace) is None)
        lab.wait('dr-target-volume-detached', 180, lambda: lab.get('volumes.longhorn.io', journal['volume'], 'longhorn-system')['status'].get('state') == 'detached')
        create(lab.client_pod(namespace, 'remount-reader', 'data', 'longhorn-node-149'))
        lab.wait('dr-target-reader-ready', 600, lambda: ready('remount-reader'))
        actual = lab.kubectl('-n', namespace, 'exec', 'remount-reader', '--', 'python3', '-c', lab.payload_program(payload, read_only=True)).stdout.strip()
        if actual != digest or lab.get('pvc', 'data', namespace)['metadata']['uid'] != claim['metadata']['uid']:
            raise ValueError('target confirmed data or PVC changed after detach/remount')
        lab.wait('dr-target-remounted-one-raw-replica', 600, lambda: replica_ready(lab, journal))
        print('LONGHORN_V2_DR_TARGET_FSYNC_REMOUNT_VERIFIED bytes='+str(len(payload.encode())*262144)+' sha256='+digest+' replicas=1 cross_node=false', flush=True)
    health(lab)
    print('LONGHORN_V2_DR_TARGET_INSTALL_BUSINESS_PASS', flush=True)


def recover(lab, pid):
    lab.core_identity(); state = lab.owner()
    if state.get('namespace_uid'):
        lab.recover_installation(pid)
    else:
        if lab.get('namespace', 'longhorn-system') or lab.get('crd', 'volumes.longhorn.io'):
            raise ValueError('target recovery refuses unowned storage resources')
        if state.get('host_state'):
            restore_hosts(lab)
    restore_resources(lab)
    print('LONGHORN_V2_DR_TARGET_INSTALL_RECOVERY_PASS core=preserved', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('core-current', 'host-inputs', 'capture-hosts', 'quiesce-multipath', 'prepare-resources',
        'verify-resources', 'restore-resources', 'restore-hosts', 'configure', 'health', 'business', 'business-clean', 'recover', 'cache'))
    parser.add_argument('--relay-pid', type=int, default=0)
    args = parser.parse_args(); target, lab = fixture(); lab.require_lease()
    if args.mode == 'host-inputs': host_inputs(target, lab)
    elif args.mode == 'prepare-resources': load('longhorn-v2-resources.py').prepare(lab)
    elif args.mode == 'business': business(lab, args.relay_pid)
    elif args.mode == 'business-clean': lab.cleanup_business(relay_pid=args.relay_pid)
    elif args.mode == 'recover': recover(lab, args.relay_pid)
    elif args.mode == 'cache': lab.cache_artifacts()
    else: {'core-current': core_current, 'capture-hosts': capture_hosts, 'quiesce-multipath': quiesce_multipath,
        'verify-resources': verify_resources, 'restore-resources': restore_resources, 'restore-hosts': restore_hosts,
        'configure': configure, 'health': health}[args.mode](lab)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        target = load('longhorn-v2-dr-target.py'); lab = target.fixture(); lab.record_failure(error)
        print('LONGHORN_V2_DR_TARGET_INSTALL_FAIL reason='+type(error).__name__, file=sys.stderr)
        sys.exit(1)
