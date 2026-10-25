#!/usr/bin/env python3
"""V1 monitoring profile: real product scrape, four alert lifecycles and bundle.

Reuse the full product monitoring lifecycle and bounded observability actions.
This scope does not repeat stability/performance or claim database acceptance.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import signal
import sys
from urllib.parse import urlsplit

NS = 'longhorn-system'
KEY = 'v1_observability'
RECORD = 'longhorn-install-business.json'


def load(name):
    spec = importlib.util.spec_from_file_location('v1_observability_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def node_names(addresses):
    return {'longhorn-node-'+a.rsplit('.', 1)[1] for a in addresses}


def filesystem_rows(lab):
    nodes = lab.get('nodes.longhorn.io', namespace=NS)['items']
    if len(nodes) != len(lab.WORKERS) or {n['metadata']['name'] for n in nodes} != node_names(lab.WORKERS):
        raise ValueError('V1 monitoring requires its original three Longhorn client nodes')
    rows = {}
    for node in nodes:
        name = node['metadata']['name']; disks = node['spec'].get('disks', {})
        if len(disks) != 1:
            raise ValueError('V1 monitoring requires one admitted filesystem disk per worker')
        key, disk = next(iter(disks.items()))
        status = node.get('status', {}).get('diskStatus', {}).get(key, {})
        maximum, available = status.get('storageMaximum'), status.get('storageAvailable')
        if (disk.get('diskType', 'filesystem') != 'filesystem' or disk.get('path') != lab.DATA or
                not disk.get('allowScheduling') or not status.get('diskUUID') or
                type(maximum) is not int or type(available) is not int or
                not 4*1024**3 <= available <= maximum):
            raise ValueError('V1 filesystem identity, capacity or four-GiB stop budget differs')
        rows[name] = {'node_uid': node['metadata']['uid'], 'disk': key, 'disk_uuid': status['diskUUID'],
            'maximum': maximum, 'available': available, 'reserved': disk.get('storageReserved', 0),
            'scheduled': status.get('storageScheduled', 0)}
    return rows


def storage_budget(lab):
    minimal = int(lab.get('settings.longhorn.io', 'storage-minimal-available-percentage', NS)['value'])
    if not 0 <= minimal <= 100:
        raise ValueError('invalid actual filesystem scheduling threshold')
    rows = filesystem_rows(lab)
    for row in rows.values():
        row['schedulable_bytes'] = min(row['available']-row['maximum']*minimal/100,
            row['maximum']-row['reserved']-row['scheduled'])
        if row['schedulable_bytes'] < 54*1024**3:
            raise ValueError('V1 monitoring lacks its worst-worker 54-GiB filesystem budget')
    return rows


def inventory_groups(lab):
    if len(lab.NODES) != 6 or tuple(lab.NODES[3:]) != tuple(lab.WORKERS):
        raise ValueError('V1 monitoring requires the leased three-control/three-storage topology')
    return {'kube_master': list(lab.NODES[:3]), 'etcd': list(lab.NODES[:3]), 'kube_node': list(lab.WORKERS)}


def scrape_health(lab, prom):
    """Manager target identity is distinct from filesystem storage series."""
    common = load('longhorn-v2-control-faults.py')
    ds = lab.get('daemonset', 'longhorn-manager', NS)
    pods = [p for p in lab.get('pods', namespace=NS)['items']
        if p['metadata'].get('labels', {}).get('app') == 'longhorn-manager']
    expected = node_names(lab.WORKERS)
    if (not ds or len(pods) != len(expected) or {p['spec'].get('nodeName') for p in pods} != expected or
            any(not common.pod_ready(p) or not common.owner(p, 'DaemonSet', ds['metadata']['uid']) for p in pods)):
        return None
    endpoints = {(p['status']['podIP'], 9500) for p in pods}
    if len(endpoints) != len(expected):
        raise ValueError('V1 manager metrics endpoints are ambiguous')
    response = prom.api('/api/v1/targets', {'state': 'active'})
    if response.get('status') != 'success':
        raise ValueError('V1 Prometheus target discovery failed')
    targets = set()
    for target in response['data']['activeTargets']:
        parsed = urlsplit(target['scrapeUrl'])
        if ((parsed.hostname, parsed.port) in endpoints and parsed.path == '/metrics' and
                target.get('labels', {}).get('namespace') == NS and target.get('health') == 'up' and
                not target.get('lastError')):
            targets.add((parsed.hostname, parsed.port))
    ready = prom.query('longhorn_node_status{condition="ready"}', allow_empty=True)
    disk = prom.query('longhorn_disk_capacity_bytes', allow_empty=True)
    if (targets != endpoints or {s['labels'].get('node') for s in ready} != expected or
            not all(s['value'] == 1 for s in ready) or
            {s['labels'].get('node') for s in disk} != node_names(lab.WORKERS) or
            not all(s['value'] > 0 for s in disk)):
        return None
    result = {'manager_targets': sorted(host+':'+str(port) for host, port in targets),
        'ready_nodes': sorted(expected), 'filesystem_nodes': sorted(node_names(lab.WORKERS)),
        'ready_samples': ready, 'filesystem_capacity_samples': disk}
    print('LONGHORN_V1_REAL_SCRAPE '+json.dumps(result, sort_keys=True), flush=True)
    return result


def monitoring():
    module = load('longhorn-v2-monitoring.py')
    module.DATA_ENGINE = 'v1'
    module.RECORD = 'longhorn-v1-monitoring.json'
    module.RELAY_HELPER = 'longhorn-lab.py'
    module.MARKER = 'LONGHORN_V1_MONITORING'
    module.storage_budget = storage_budget
    module.inventory_groups = inventory_groups
    module.scrape_health = scrape_health
    original_cache = module.cache
    module.cache = lambda lab, **options: load('longhorn-v1-monitoring-cache.py').stage(
        lab, lambda current: original_cache(current, **options))
    return module


SHARED = load('longhorn-v2-observability.py')
SHARED.KEY = KEY


class Probe(SHARED.Probe):
    DATA_ENGINE = 'v1'

    def manager_nodes(self):
        return self.lab.WORKERS

    def __init__(self, lab, pid, store, configuration, *, observe=True):
        super().__init__(lab, pid, store, configuration, monitoring=monitoring(), observe=observe)

    def emit(self, marker, row):
        if marker == 'LONGHORN_V2_BOUNDED_CAPACITY':
            row = dict(row, boundary='512MiB fsync file in the business PVC, native trim and original filesystem disk counters; bounded fixture threshold, not a shared-host full-disk test.')
        super().emit(marker.replace('LONGHORN_V2_', 'LONGHORN_V1_'), row)

    def disk_used(self):
        rows = filesystem_rows(self.lab)
        previous = self.journal[KEY].get('filesystem_disks')
        identity = {name: {k: row[k] for k in ('node_uid', 'disk', 'disk_uuid', 'maximum')} for name, row in rows.items()}
        if previous and identity != previous:
            raise ValueError('V1 original filesystem disk identity changed during capacity observation')
        if not previous:
            self.journal[KEY]['filesystem_disks'] = identity
            SHARED.persist(self.lab, self.journal)
        return {name: row['maximum']-row['available'] for name, row in rows.items()}

    def network_fault(self):
        allowed = self.owned_consumers()
        fault = load('longhorn-v1-storage-faults.py')
        control = load('longhorn-control-faults.py')
        # Upstream detects stalled replica I/O through engine-replica-timeout.
        # Reuse the confirmed fsync writer so this bounded gate does not depend
        # on an idle connection's transport-level failure detection.
        def inject(lab, journal, variant):
            proof = fault.inject(lab, journal, 'network', allowed_consumers=allowed)
            self.alert('Degraded', True)
            fault.restore_fault(lab, journal, proof)
            return proof
        control.inject = inject
        control.recovered = lambda lab, journal, proof: fault.recovered(lab, journal, proof, emit=False)
        control.verify(self.lab, self.journal, None, 'instance-manager')
        proof = self.journal[fault.KEY]
        self.alert('Degraded', False)
        self.emit('LONGHORN_V1_NATIVE_DEGRADED_RECOVERED', {'volume_uid': self.journal['volume_uid'],
            'replica_uid': proof['replica_uid'], 'replicas': proof['recovered']['replicas'],
            'fault_restored': proof['fault_restored'], 'degraded': proof['degraded']})

    def restore_network(self):
        fault = load('longhorn-v1-storage-faults.py')
        proof = self.journal.get(fault.KEY)
        if proof:
            fault.restore_fault(self.lab, self.journal, proof)
            self.lab.wait('v1-observability-fault-restored-before-cleanup', 1200,
                lambda: fault.recovered(self.lab, self.journal, proof, emit=False))

    def run(self, journal, payload, digest):
        self.journal = journal
        if KEY in journal:
            raise ValueError('prior V1 observability fixture requires owned recovery')
        journal[KEY] = {'run_id': self.lab.owner()['run_id'], 'volume_uid': journal['volume_uid']}
        SHARED.persist(self.lab, journal)
        self.rules(); self.network_fault(); self.pressure(); self.backup(); self.missing(); self.retire_rules()
        self.support_bundle()
        actual = self.lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-c',
            self.lab.payload_program(payload, read_only=True), timeout=90).stdout.strip()
        if actual != digest or not self.lab.replica_topology(journal, 3):
            raise ValueError('V1 observability actions changed confirmed bytes or replicas')
        self.emit('LONGHORN_V1_OBSERVABILITY_SOURCE_VERIFIED', {'volume_uid': journal['volume_uid'],
            'bytes': len(payload)*262144, 'sha256': digest,
            'boundary': 'Bounded alert lifecycle and original payload; stability and performance remain separate matrix scopes.'})


def support_business(lab, pid):
    """Reach the same native bundle path without redeploying unrelated monitoring."""
    probe = Probe(lab, pid, None, None, observe=False)
    original_hook, original_cleanup = lab.verify_performance, lab.cleanup_business
    def run(journal, payload, digest):
        if KEY in journal:
            raise ValueError('prior V1 support fixture requires owned recovery')
        probe.journal = journal
        journal[KEY] = {'run_id': lab.owner()['run_id'], 'volume_uid': journal['volume_uid'], 'support_focus': True}
        SHARED.persist(lab, journal)
        probe.support_bundle()
        actual = lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-c',
            lab.payload_program(payload, read_only=True), timeout=90).stdout.strip()
        if actual != digest or not lab.replica_topology(journal, 3):
            raise ValueError('support bundle changed confirmed bytes or replicas')
        probe.emit('LONGHORN_V1_SUPPORT_SOURCE_VERIFIED', {'volume_uid': journal['volume_uid'],
            'bytes': len(payload)*262144, 'sha256': digest})
    def clean(record=None, **kwargs):
        probe.restore()
        return original_cleanup(record, **kwargs)
    lab.verify_performance, lab.cleanup_business = run, clean
    try:
        lab.business('performance', relay_pid=pid, storage_class='longhorn-delete')
    finally:
        lab.verify_performance, lab.cleanup_business = original_hook, original_cleanup
    print('LONGHORN_V1_SUPPORT_BUSINESS_PASS', flush=True)


def verify_support_log(log):
    lines = log.splitlines()
    def one(marker):
        rows = [json.loads(line.split(' ', 1)[1]) for line in lines if line.startswith(marker+' ')]
        if len(rows) != 1:
            raise ValueError('support focus requires one '+marker)
        return rows[0]
    source = one('LONGHORN_V1_SUPPORT_SOURCE_VERIFIED')
    bundle = one('LONGHORN_V1_SUPPORT_BUNDLE_BOUNDARY')
    metadata = one('LONGHORN_V1_SUPPORT_BUNDLE_METADATA')
    if (not source.get('volume_uid') or not 0 < source.get('bytes', 0) or not re.fullmatch('[a-f0-9]{64}', source.get('sha256', '')) or
            bundle.get('crc_verified') is not True or bundle.get('extracted') is not False or bundle.get('published') is not False or
            not bundle.get('source_uid') or metadata.get('source_uid') != bundle['source_uid'] or
            metadata.get('state') != 'ReadyForDownload' or metadata.get('compressed_bytes') != bundle.get('compressed_bytes') or
            not 0 < bundle.get('compressed_bytes', 0) <= SHARED.SUPPORT_COMPRESSED_BYTES or not 0 < bundle.get('expanded_bytes', 0) <= SHARED.SUPPORT_EXPANDED_BYTES or
            not 0 < bundle.get('members', 0) <= 10000 or not re.fullmatch('[a-f0-9]{64}', bundle.get('sha256', ''))):
        raise ValueError('support focus metadata, source or private archive verification differs')
    remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    workers = node_names(('192.168.122.212', '192.168.122.165', '192.168.122.238'))
    if (len(remounts) != 1 or int(remounts[0][0]) != source['bytes'] or remounts[0][1] != source['sha256'] or
            set(remounts[0][2].split(',')) != workers):
        raise ValueError('support focus source was not remounted across the original workers')
    if any(lines.count(marker) != 1 for marker in ('LONGHORN_V1_SUPPORT_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')):
        raise ValueError('support focus business or cleanup is incomplete')
    if re.search(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', log, re.M):
        raise ValueError('support focus contains a failure marker')
    print('LONGHORN_V1_SUPPORT_LOG_VERIFIED', flush=True)


def business(lab, pid):
    monitor = monitoring(); monitor.verify(lab, pid)
    store = lab.backup_store()
    with lab.scoped_cleanup(store.cleanup):
        store.prepare(); store.qualify()
        probe = Probe(lab, pid, store, store.product_configuration())
        original_hook, original_cleanup = lab.verify_performance, lab.cleanup_business
        lab.verify_performance = probe.run
        def clean(record=None, **kwargs):
            probe.restore()
            return original_cleanup(record, **kwargs)
        lab.cleanup_business = clean
        try:
            # Reuse the existing business callback boundary and cross-node
            # fsync remount, without invoking its performance benchmark.
            lab.business('performance', relay_pid=pid, storage_class='longhorn-delete')
        finally:
            lab.verify_performance, lab.cleanup_business = original_hook, original_cleanup
    print('LONGHORN_V1_OBSERVABILITY_BUSINESS_PASS', flush=True)


def cleanup(lab, pid):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('V1 observability refuses a symlink business journal')
    if path.exists():
        journal = json.loads(path.read_text()); proof = journal.get(KEY, {})
        if proof.get('run_id') != lab.owner()['run_id'] or proof.get('volume_uid') != journal.get('volume_uid'):
            raise ValueError('V1 observability refuses another business journal')
        lab.check_business_graph(journal)
        probe = Probe(lab, pid, lab.backup_store(), None, observe=not proof.get('support_focus', False)); probe.journal = journal
        probe.restore(); lab.cleanup_business(relay_pid=pid)
    lab.backup_store().cleanup()
    if path.exists():
        raise ValueError('V1 observability business journal remains after cleanup')
    print('LONGHORN_V1_OBSERVABILITY_BUSINESS_SCOPED_CLEAN_PASS', flush=True)


def baseline(lab, pid=0):
    lab.core_identity()
    load('longhorn-platform.py').empty(lab)
    lab.installation_health()
    rows = filesystem_rows(lab)
    if (lab.DIRECTORY/'longhorn-v1-monitoring.json').exists():
        raise ValueError('V1 empty monitoring baseline still has its ownership receipt')
    print('LONGHORN_V1_OBSERVABILITY_BASELINE_VERIFIED '+json.dumps(rows, sort_keys=True), flush=True)


def verify_log(log):
    lines = log.splitlines()
    if re.search(r'^(LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', log, re.M):
        raise ValueError('V1 observability log contains a failure')
    def one(marker):
        rows = [json.loads(line[len(marker)+1:]) for line in lines if line.startswith(marker+' ')]
        if len(rows) != 1:
            raise ValueError('V1 observability requires one complete observation: '+marker)
        return rows[0]
    source = one('LONGHORN_V1_OBSERVABILITY_SOURCE_VERIFIED')
    scrape = one('LONGHORN_V1_REAL_SCRAPE')
    expected_workers = node_names(('192.168.122.212', '192.168.122.165', '192.168.122.238'))
    if (set(scrape.get('ready_nodes', [])) != expected_workers or
            set(scrape.get('filesystem_nodes', [])) != expected_workers or
            len(set(scrape.get('manager_targets', []))) != 3 or
            {r['labels'].get('node') for r in scrape.get('ready_samples', [])} != expected_workers or
            not all(r['value'] == 1 for r in scrape['ready_samples']) or
            {r['labels'].get('node') for r in scrape.get('filesystem_capacity_samples', [])} != expected_workers or
            not all(r['value'] > 0 for r in scrape['filesystem_capacity_samples'])):
        raise ValueError('V1 real scrape omits an original manager, node or filesystem disk')
    if (not source.get('volume_uid') or type(source.get('bytes')) is not int or source['bytes'] <= 0 or
            not re.fullmatch('[a-f0-9]{64}', source.get('sha256', ''))):
        raise ValueError('V1 original confirmed-byte identity is incomplete')
    volume_uid = source['volume_uid']; rules = one('LONGHORN_V1_REAL_PROMETHEUS_RULES')
    if rules['volume_uid'] != volume_uid or not rules.get('sample_probe') or rules['bounded_space_threshold_bytes'] <= 0:
        raise ValueError('V1 real alert-rule identity differs')
    alerts = [json.loads(line.split(' ', 1)[1]) for line in lines if line.startswith('LONGHORN_V1_REAL_ALERT ')]
    for suffix in ('Degraded', 'Space', 'Backup', 'Missing'):
        rows = [r for r in alerts if r['alert'] == rules['alert_prefix']+suffix]
        active = [r for r in rows if r.get('active') is True]; restored = [r for r in rows if r.get('active') is False]
        if (not active or not restored or max(r['observed_at'] for r in restored) <= max(r['observed_at'] for r in active) or
                any(r.get('volume_uid') != volume_uid for r in rows)):
            raise ValueError('V1 real alert did not fire and subsequently resolve: '+suffix)
    fault = one('LONGHORN_V1_NATIVE_DEGRADED_RECOVERED')
    if (fault['volume_uid'] != volume_uid or fault.get('fault_restored') is not True or
            fault.get('degraded', {}).get('volume_robustness') != 'degraded' or
            len(fault.get('replicas', {})) != 3 or
            {r['node'] for r in fault['replicas'].values()} != expected_workers):
        raise ValueError('V1 monitoring lacks restored native three-replica degradation')
    capacity = one('LONGHORN_V1_BOUNDED_CAPACITY')
    if (capacity.get('filler_bytes') != 512*1024**2 or not re.fullmatch('[a-f0-9]{64}', capacity['filler_sha256']) or
            set(capacity['before']) != expected_workers or set(capacity['peak']) != expected_workers or
            set(capacity['after']) != expected_workers or any(capacity['peak'][n]-capacity['before'][n] < 256*1024**2 or
                capacity['peak'][n]-capacity['after'][n] < 256*1024**2 for n in capacity['before'])):
        raise ValueError('V1 filesystem capacity alert lacks bounded real allocation and reclamation')
    backup = one('LONGHORN_V1_BACKUP_ALERT_RECOVERED')
    if backup['volume_uid'] != volume_uid or not backup.get('backup_uid') or not re.fullmatch('[a-f0-9]{64}', backup['object_receipt_sha256']):
        raise ValueError('V1 backup alert lacks completed product backup and object receipt')
    bundle = one('LONGHORN_V1_SUPPORT_BUNDLE_BOUNDARY')
    if (bundle.get('crc_verified') is not True or bundle.get('extracted') is not False or bundle.get('published') is not False or
            not bundle.get('source_uid') or not 0 < bundle.get('compressed_bytes', 0) <= SHARED.SUPPORT_COMPRESSED_BYTES or
            not 0 < bundle.get('expanded_bytes', 0) <= SHARED.SUPPORT_EXPANDED_BYTES or
            not 0 < bundle.get('members', 0) <= 10000 or not re.fullmatch('[a-f0-9]{64}', bundle['sha256'])):
        raise ValueError('V1 support bundle boundary or private archive verification differs')
    remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    if (len(remounts) != 1 or int(remounts[0][0]) != source['bytes'] or remounts[0][1] != source['sha256'] or
            set(remounts[0][2].split(',')) != {r['node'] for r in fault['replicas'].values()}):
        raise ValueError('V1 observability original confirmed bytes were not remounted across workers')
    for marker in ('LONGHORN_V1_OBSERVABILITY_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS'):
        if lines.count(marker) != 1:
            raise ValueError('V1 observability business/cleanup marker is incomplete')
    print('LONGHORN_V1_OBSERVABILITY_LOG_VERIFIED', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('image-cache', 'baseline', 'admission', 'setup', 'monitor-verify', 'monitor-cleanup', 'business', 'support-business', 'cleanup', 'verify', 'verify-support'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.action in ('verify', 'verify-support'):
        if not args.log: parser.error('--log is required')
        (verify_log if args.action == 'verify' else verify_support_log)(args.log.read_text()); return
    if args.relay_pid <= 0 and args.action != 'image-cache': parser.error('a supervised fixed Registry relay is required')
    lab = load('longhorn-lab.py'); sys.modules[lab.__name__] = lab; lab.require_lease()
    if args.action == 'image-cache':
        monitoring().cache(lab)
        return
    if args.action in ('baseline', 'business', 'cleanup', 'support-business'):
        globals()[args.action.replace('support-business', 'support_business')](lab, args.relay_pid)
    else:
        getattr(monitoring(), {'monitor-verify': 'verify', 'monitor-cleanup': 'cleanup'}.get(args.action, args.action))(lab, args.relay_pid)


if __name__ == '__main__':
    for value in (signal.SIGINT, signal.SIGTERM):
        signal.signal(value, lambda number, frame: sys.exit(128+number))
    main()
