#!/usr/bin/env python3
"""Recover exact Longhorn scenario owners; retain healthy clusters and disks.

This is LONGHORN-41's recovery entry, not LONGHORN-54's native retirement or
the full-delivery clean exit. It never discovers ownership from API names.
Unknown journals and overlapping owners stop before the first mutation.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

BASE = Path('/usr/local/kubeauto')
CLUSTERS = {'v1': 'longhorn-gate', 'v2': 'longhorn-v2-gate', 'target': 'longhorn-v2-dr-target'}
NODES = {'v1': ('192.168.122.135', '192.168.122.40', '192.168.122.72',
    '192.168.122.212', '192.168.122.165', '192.168.122.238'),
    'v2': ('192.168.47.146', '192.168.47.147', '192.168.47.148'), 'target': ('192.168.47.149',)}
BUSINESS = 'longhorn-install-business.json'
COMMON = {BUSINESS, 'longhorn-request-input.json', 'longhorn-encryption-key.json',
    'longhorn-security-tls.json', 'longhorn-network-probes.json', 'longhorn-reclaim-class.json',
    'longhorn-backup-refusal.json', 'longhorn-backup-store.json',
    'longhorn-recurring-credentials.json', 'longhorn-recurring-runtime.json',
    'longhorn-recurring-qualification.json', 'longhorn-ownership-csi.json',
    'longhorn-ownership-preinstall.json', 'longhorn-ownership-class.json',
    'longhorn-ownership-controller.json', 'longhorn-ownership-request.json'}
PASSIVE = {'longhorn-lab-owner.json', 'longhorn-idempotence-observation.json',
    'longhorn-uninstall-events.json', 'longhorn-last-replica-complete.json',
    'longhorn-platform-service-refusal-output.json'}
# These are exact archive names produced by the original owner modules.
ARCHIVE = re.compile(r'longhorn-(?:platform-recovered|node-failure-recovered|capacity-recovered|'
    r'integrity-recovered|maintenance-recovered|upgrade-result|system-recovery-result|'
    r'v[12]-upgrade-preparation-complete|v2-disk-lifecycle-completed|v2-runtime-recovered|'
    r'v2-cpu-isolation-completed|v2-resources-restored|v2-resources-pre-reboot|'
    r'v2-host-state-pre-reboot|v2-boot-fixture-completed)-[0-9a-f-]+\.json\Z|'
    r'longhorn-v2-resources-reboot-complete-[0-9a-f]{64}\.json\Z|'
    r'longhorn-account-failure-[0-9a-f]{24}\.json\Z|'
    r'longhorn-(?:system-recovery-prebusiness-cleaned|v1-upgrade-before-business)-[0-9a-f]{32}\.json\Z|'
    r'longhorn-v2-resources-ambiguous-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.json\Z|'
    r'longhorn-(?:v1|v2|v2-dr-target)-im-adopt-rejected-setup-[0-9a-f]{16}\.json\Z|'
    r'longhorn-v2-boot-[0-9a-f-]+-(?:resources|host-state)\.json\Z')
FAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)')


@dataclass(frozen=True)
class Route:
    name: str
    scope: str
    files: frozenset
    helper: str
    mode: str
    shell: bool = True
    target: bool = False


def route(name, scope, files, helper, mode, *, shell=True, target=False):
    return Route(name, scope, frozenset(files.split()), helper, mode, shell, target)


# Primary journals select their original cleanup, never the business fallback.
ROUTES = (
    route('platform', 'v1', 'longhorn-platform.json', 'longhorn-platform.py', 'clean', shell=False),
    route('system-restore', 'v1', 'longhorn-system-recovery.json', 'longhorn-system-recovery.sh', 'system-restore-cleanup', target=True),
    route('disaster-recovery', 'v1', 'longhorn-v1-dr.json', 'longhorn-v1-disaster.sh', 'disaster-recovery-cleanup', target=True),
    route('upgrade', 'v1', 'longhorn-v1-upgrade-lifecycle.json longhorn-upgrade-lifecycle.json', 'longhorn-v1-upgrade-lifecycle.sh', 'v1-upgrade-cleanup', target=True),
    route('node-failure', 'v1', 'longhorn-node-failure.json longhorn-node-failure-business.json', 'longhorn-node-failure.sh', 'node-failure-cleanup'),
    route('maintenance', 'v1', 'longhorn-maintenance.json longhorn-maintenance-business.json', 'longhorn-maintenance.sh', 'maintenance-cleanup'),
    route('node-lifecycle', 'v1', 'longhorn-node-lifecycle.json longhorn-node-lifecycle-business.json', 'longhorn-node-lifecycle.sh', 'node-lifecycle-cleanup'),
    route('disk-lifecycle', 'v1', 'longhorn-disk-lifecycle.json longhorn-disk-lifecycle-business.json', 'longhorn-disk-lifecycle.sh', 'disk-lifecycle-cleanup'),
    route('capacity', 'v1', 'longhorn-capacity.json longhorn-capacity-business.json', 'longhorn-capacity.sh', 'capacity-cleanup'),
    route('integrity', 'v1', 'longhorn-integrity.json', 'longhorn-integrity.sh', 'integrity-cleanup'),
    route('middleware', 'v1', 'longhorn-v1-middleware.json', 'longhorn-v1-middleware.sh', 'middleware-cleanup'),
    route('last-replica', 'v1', 'longhorn-last-replica-intent.json', 'longhorn-last-replica.sh', 'last-replica-cleanup'),
    route('disk-lifecycle', 'v2', 'longhorn-v2-disk-lifecycle.json longhorn-v2-disk-lifecycle-business.json', 'longhorn-v2-disk-lifecycle.sh', 'v2-disk-lifecycle-cleanup'),
    route('upgrade', 'v2', 'longhorn-upgrade-lifecycle.json', 'longhorn-v2-upgrade-lifecycle.sh', 'v2-upgrade-cleanup', target=True),
    route('coexist', 'v2', 'longhorn-v2-coexist.json longhorn-coexist-business-v1.json longhorn-coexist-business-v2.json', 'longhorn-v2-coexist.sh', 'v2-coexist-cleanup'),
    route('maintenance', 'v2', 'longhorn-v2-maintenance-drain.json longhorn-v2-maintenance-drain-business.json', 'longhorn-v2-maintenance-drain.sh', 'v2-maintenance-drain-cleanup'),
    route('runtime-resources', 'v2', 'longhorn-v2-runtime-resources.json', 'longhorn-v2-runtime-resources.sh', 'v2-runtime-resources-cleanup'),
    route('interrupt', 'v2', 'longhorn-v2-interrupt.json', 'longhorn-v2-volumes.sh', 'v2-interrupt-cleanup'),
    route('incomplete-clone', 'v2', 'longhorn-v2-incomplete-clone.json', 'longhorn-v2-incomplete-clone.sh', 'v2-incomplete-clone-cleanup'),
    route('linked-clones', 'v2', 'longhorn-v2-linked-clones.json', 'longhorn-v2-linked-clones.sh', 'v2-linked-clone-cleanup'),
    route('linked-lifecycle', 'v2', 'longhorn-v2-linked-lifecycle.json', 'longhorn-v2-linked-lifecycle.sh', 'v2-linked-lifecycle-cleanup'),
    route('broken-restore', 'v2', 'longhorn-v2-broken-restore.json', 'longhorn-v2-broken-restore.sh', 'v2-broken-restore-cleanup'),
    route('systembackup', 'v2', 'longhorn-systembackup-qualification.json', 'longhorn-v2-systembackup.sh', 'v2-systembackup-cleanup'),
    route('systemrestore', 'v2', 'longhorn-systemrestore-qualification.json longhorn-systemrestore-business.json', 'longhorn-v2-systemrestore.py', 'cleanup', shell=False),
    route('detached', 'v2', 'longhorn-v2-detached-setting.json', 'longhorn-v2-detached-backup.py', 'cleanup', shell=False),
    route('disaster-recovery', 'v2', 'longhorn-v2-dr.json longhorn-dr-failback-business.json', 'longhorn-v2-dr-failover.py', 'cleanup', shell=False, target=True),
)
FAULTS = {
    'v1': {'v1_storage_faults': ('stability', 'longhorn-v1-stability.sh', 'stability-cleanup'),
        'v1_last_replica': ('last-replica', 'longhorn-last-replica.sh', 'last-replica-cleanup'),
        'v1_observability': ('observability', 'longhorn-v1-observability.sh', 'observability-cleanup')},
    'v2': {key: ('failure', 'longhorn-v2-failure.sh', 'v2-failure-cleanup') for key in
        ('v2_all_replicas_fault', 'v2_disk_fault', 'v2_runtime_fault', 'v2_manager_loss', 'v2_replica_im_loss')},
    'target': {}}
FAULTS['v2']['v2_observability'] = ('observability', 'longhorn-v2-observability.sh', 'v2-observability-cleanup')
# An ordinary business journal has no host/component recovery operation. Reject
# new fields until their actual owner is reviewed; do not guess a future fault.
BUSINESS_FIELDS = set('namespace namespace_uid objects defaults storage_class reclaim_policy '
    'volume volume_uid pv pv_uid pvc_uid snapshots native_snapshots backups backup_volume claims '
    'rebind retained performance control_fault integrity trim source_retired encrypted_backup_device '
    'expansions source_class last_replica_deletion_guard'.split())
RETAIN = {
    'longhorn-v2-boot-fixture.json': 'worker-reboot owner must restore its exact allocation before business cleanup',
    'longhorn-v1-recovery-target.json': 'recover the independent target through its V1 source coordinator',
    'longhorn-dr-backup-consumer.json': 'recover the independent target through its backup coordinator',
    'longhorn-v2-post-reboot-environment.json': 'diagnose the saved multipath/environment transition; its owner refuses replay',
    'longhorn-v2-installed-post-reboot-environment.json': 'diagnose the saved current-installation multipath transition; its owner refuses replay',
    'longhorn-v2-resources-reboot.json': 'diagnose the saved page-pool transition; its owner refuses replay',
}
WORKLOAD = re.compile(r'longhorn-upgrade-business-[a-z0-9-]+\.json\Z')


def read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('journal must be an owner-private regular file: '+path.name)
    value = json.loads(path.read_text())
    if not isinstance(value, dict): raise ValueError('journal must be a mapping: '+path.name)
    return value


def bind(row, owner, name):
    for key in ('run_id', 'cluster_uid'):
        if key in row and row[key] != owner.get(key): raise ValueError('foreign journal '+name+' '+key)
    for key in ('source_core',):
        if isinstance(row.get(key), dict): bind(row[key], owner, name+'/'+key)


def scan(base):
    """Read all three scopes before deciding any recovery action."""
    result = {}
    for scope, cluster in CLUSTERS.items():
        directory = base/'clusters'/cluster
        if directory.is_symlink(): raise ValueError('symlink cluster directory')
        if not directory.exists(): continue
        owner = read(directory/'longhorn-lab-owner.json')
        if (owner.get('cluster') != cluster or tuple(owner.get('nodes', ())) != NODES[scope] or
                owner.get('clean_start_verified') is not True):
            raise ValueError('foreign cluster owner: '+scope)
        uuid.UUID(owner['run_id'])
        records = {}
        for path in sorted(directory.glob('longhorn-*.json')):
            aborted = re.fullmatch(r'longhorn-upgrade-aborted-([0-9a-f]{32})\.json', path.name)
            if aborted:
                row = read(path); identity = row.get('source_identity', {})
                if (scope != 'v2' or row.get('id') != aborted[1] or
                        row.get('stage') != 'aborted-before-upgrade' or
                        row.get('old_runtime', {}).get('version') != '1.12.0' or
                        any(key in row for key in ('new_runtime', 'legacy', 'retained_before', 'successful_configuration')) or
                        any(identity.get(key) != owner.get(key) for key in ('run_id', 'cluster_uid', 'node_uids'))):
                    raise ValueError('incomplete or foreign upgrade abort archive')
                continue
            # Passive diagnostics need not be mappings (uninstall Events are a
            # list). Preserve original regular owned files without requiring
            # private permissions from historical diagnostic writers.
            if path.name in PASSIVE or ARCHIVE.fullmatch(path.name):
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                    raise ValueError('unsafe passive journal: '+path.name)
                continue
            row = read(path); bind(row, owner, path.name)
            records[path.name] = row
        result[scope] = (owner, records)
    return result


def plan(base):
    scopes = scan(base); selected = []; consumed_target = False
    for scope in ('v1', 'v2', 'target'):
        if scope not in scopes: continue
        owner, records = scopes[scope]; names = set(records)
        if 'longhorn-v2-cpu-isolation.json' in names and scope != 'v2':
            raise ValueError('V2 CPU owner on a foreign scope')
        if 'longhorn-v2-resources.json' in names and scope == 'v1':
            raise ValueError('V2 resource owner on a foreign scope')
        if scope == 'target' and consumed_target:
            allowed = COMMON | {'longhorn-dr-backup-consumer.json', 'longhorn-v1-recovery-target.json',
                'longhorn-v2-resources.json'}
            unknown = {n for n in names-allowed if not WORKLOAD.fullmatch(n)}
            if unknown: raise ValueError('target has a conflicting journal: '+','.join(sorted(unknown)))
            continue
        # Preparation/adoption journals carry a version transition. Recovery
        # must never turn a partial upgrade into an accepted new baseline.
        for name, row in records.items():
            if name.endswith('-upgrade-preparation.json'):
                if row.get('stage') != 'complete':
                    raise ValueError('incomplete upgrade preparation retained: '+name+'; use its owning upgrade recovery')
                names.remove(name)
            elif re.fullmatch(r'longhorn-(?:v1|v2|v2-dr-target)-im-(?:revision-[23]-)?adopt\.json', name):
                raise ValueError('incomplete image adoption retained: '+name+'; use its owning adoption recovery')
            elif name in ('longhorn-v2-post-reboot-environment.json',
                          'longhorn-v2-installed-post-reboot-environment.json', 'longhorn-v2-resources-reboot.json'):
                if row.get('phase') != 'complete': raise ValueError(RETAIN[name])
                names.remove(name)
            elif name == 'longhorn-v2-cpu-isolation.json' and row.get('clean') is True:
                if (set(row.get('hosts', {})) != set(NODES['v2']) or
                        not all(h.get('captured') and h.get('restored') for h in row['hosts'].values())):
                    raise ValueError('incomplete CPU isolation recovery receipt')
                names.remove(name)
            elif name == 'longhorn-busybox-cache-reconcile.json':
                proof = row.get('proof', {})
                if (not isinstance(proof, dict) or row.get('phase') not in ('verified', 'restored') or row.get('copy_rc') != 0 or
                        proof.get('run_id') != owner['run_id'] or proof.get('key') != 'brinnatt/busybox:1.37'):
                    raise ValueError('incomplete or foreign single-key cache recovery receipt')
                names.remove(name)
            elif name == 'longhorn-relay-recovery.json':
                if owner.get('relays'): raise ValueError('pre-core legacy relay journal requires registry-recover')
                names.remove(name)
        if 'longhorn-v2-resources.json' in names:
            # The admitted page pool is a healthy installation prerequisite.
            # LONGHORN-54 alone restores it after native disk/IM retirement.
            if not owner.get('namespace_uid'):
                raise ValueError('uninstalled V2 page pool retained; use the owning resource/host recovery')
            names.remove('longhorn-v2-resources.json')
        routes = [r for r in ROUTES if r.scope == scope and r.files & names]
        business = records.get(BUSINESS, {})
        if business:
            known = BUSINESS_FIELDS | set(FAULTS['v1']) | set(FAULTS['v2']) | {
                'v1_storage_fault','v1_storage_fault_intent','v2_manager_recovered','v2_replica_im_recovered'}
            if set(business)-known: raise ValueError('unknown business owner fields: '+','.join(sorted(set(business)-known)))
            if not business.get('namespace', '').startswith('longhorn-business-') or not business.get('namespace_uid'):
                raise ValueError('foreign business namespace')
        choices = [(*value, key) for key, value in FAULTS[scope].items() if key in business]
        for key in ('v1_storage_fault', 'v1_storage_fault_intent'):
            if key in business:
                if scope != 'v1': raise ValueError('V1 fault on a foreign cluster')
                case = business[key].get('case')
                if case not in ('disk', 'network'): raise ValueError('unknown V1 fault case')
                choices.append((case+'-failure', 'longhorn-v1-storage-faults.sh', case+'-failure-cleanup', key))
        # Fault intent and its populated proof are one owner, not two owners.
        fault_owners = {(n,h,m) for n,h,m,_ in choices}
        keys = {key for *_, key in choices}
        if len(fault_owners) > 1 or len(keys) > 1 and keys != {'v1_storage_fault','v1_storage_fault_intent'}:
            raise ValueError('conflicting business fault owners: '+scope)
        for n,h,m in fault_owners:
            if not any(r.name == n for r in routes): routes.append(route(n, scope, BUSINESS, h, m))
        for _,_,_,key in choices: bind(business[key], owner, key)
        if 'longhorn-v2-runtime-resources.json' in names:
            routes = [r for r in routes if r.name != 'interrupt']
        if 'longhorn-systemrestore-qualification.json' in names:
            routes = [r for r in routes if r.name != 'systembackup']
        # A retained monitoring installation may coexist with its database
        # consumer. Fault/business recovery must precede monitoring deletion.
        monitor = 'longhorn-v1-monitoring.json' if scope == 'v1' else 'longhorn-v2-monitoring.json'
        if scope != 'target' and monitor in names and not any(r.name == 'observability' for r in routes):
            routes.append(route('monitor', scope, monitor,
                'longhorn-v1-observability.py' if scope == 'v1' else 'longhorn-v2-monitoring.py',
                'monitor-cleanup' if scope == 'v1' else 'cleanup', shell=False))
        if 'longhorn-v2-cpu-isolation.json' in names:
            routes.insert(0, route('cpu', scope, 'longhorn-v2-cpu-isolation.json', 'longhorn-v2-cpu-isolation.py', 'clean', shell=False))
        main = [r for r in routes if r.name not in ('monitor', 'cpu')]
        if len(main) > 1: raise ValueError('conflicting primary scenario owners: '+scope)
        if any(r.name == 'cpu' for r in routes) and any(r.name not in ('cpu','monitor','observability') for r in routes):
            raise ValueError('CPU profile overlaps another scenario owner')
        covered = set().union(*(r.files for r in routes)) if routes else set()
        if any(r.name == 'observability' for r in routes): covered.add(monitor)
        if any(r.name == 'runtime-resources' for r in routes): covered.add('longhorn-v2-interrupt.json')
        if any(r.name == 'systemrestore' for r in routes): covered.add('longhorn-systembackup-qualification.json')
        if any(r.name == 'failure' for r in routes): covered.add('longhorn-v2-boot-fixture.json')
        if any(r.target for r in routes): covered.update(n for n in names if WORKLOAD.fullmatch(n))
        ordinary = names - covered
        for name in ordinary:
            if name in RETAIN: raise ValueError(RETAIN[name])
        if ordinary-COMMON:
            raise ValueError('unknown or orphan journal: '+','.join(sorted(ordinary-COMMON)))
        if BUSINESS in ordinary and not choices and set(business)-BUSINESS_FIELDS:
            raise ValueError('unknown business owner fields: '+','.join(sorted(set(business)-BUSINESS_FIELDS)))
        # These owners consume common request/business/backup journals as part
        # of their own restore ordering. Never delete them ahead of that owner.
        owns_common = {'platform','system-restore','disaster-recovery','upgrade','integrity','last-replica',
            'stability','disk-failure','network-failure','observability','failure','runtime-resources',
            'interrupt','incomplete-clone','linked-clones','linked-lifecycle','broken-restore','systembackup','systemrestore','detached'}
        if ordinary:
            if main and main[0].name not in owns_common:
                raise ValueError('unrelated common business journal overlaps '+main[0].name)
            if not main:
                if scope == 'target': raise ValueError('orphan target consumer; recover through its source coordinator')
                routes.insert(0, route('business', scope, ' '.join(sorted(ordinary)), '', '', shell=False))
        for r in routes:
            if r.target:
                if consumed_target: raise ValueError('two independent coordinators claim the DR target')
                if 'target' not in scopes: raise ValueError('coordinator requires its original DR target')
                consumed_target = True
        selected.extend(routes)
    return scopes, selected


def load(name):
    spec = importlib.util.spec_from_file_location('cleanup_'+name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    return module


def fixture(scope):
    if scope == 'v1': return load('longhorn-lab.py')
    # The V2 backup profile overrides nested cleanup's fixed endpoints.
    # Loading plain install here would restore against the V1 backup service.
    return load('longhorn-v2-backups.py' if scope == 'v2' else 'longhorn-v2-dr-target-install.py').fixture()[1]


def expected_marker(action):
    special = {
        ('v1', 'platform'): 'LONGHORN_PLATFORM_FIXTURES_RESTORED',
        ('v1', 'stability'): 'LONGHORN_STABILITY_SCOPED_CLEAN_PASS',
        ('v1', 'monitor'): 'LONGHORN_V1_MONITORING_SCOPED_CLEAN_PASS',
        ('v2', 'monitor'): 'LONGHORN_V2_MONITORING_SCOPED_CLEAN_PASS',
        ('v2', 'cpu'): 'LONGHORN_V2_CPU_ISOLATION_SCOPED_CLEAN_PASS',
        ('v2', 'observability'): 'LONGHORN_V2_OBSERVABILITY_PERFORMANCE_SCOPED_CLEAN_PASS',
        ('v2', 'systemrestore'): 'LONGHORN_V2_SYSTEMRESTORE_FIXTURES_CLEAN_PASS',
        ('v2', 'detached'): 'LONGHORN_V2_DETACHED_SETTING_RESTORED',
        ('v2', 'disaster-recovery'): 'LONGHORN_V2_DR_FAILOVER_FIXTURES_CLEAN_PASS',
    }
    return special.get((action.scope, action.name), 'LONGHORN_'+action.mode.upper().replace('-', '_')+'_PASS')


def run(argv, log, expected):
    """Bound one owned child; preserve the first failure and its exact log."""
    with log.open('xb') as output:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True, pass_fds=(7,8,9))
        started = heartbeat = time.monotonic(); cursor = 0
        try:
            while True:
                with log.open('rb') as stream:
                    stream.seek(cursor); data = stream.read(); cursor += len(data)
                if data: sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
                rc = process.poll()
                if rc is not None:
                    with log.open('rb') as stream:
                        stream.seek(cursor); final = stream.read()
                    if final: sys.stdout.buffer.write(final); sys.stdout.buffer.flush()
                    break
                now = time.monotonic()
                if now-started > 7200: raise TimeoutError('owner cleanup exceeded two hours')
                if now-heartbeat > 15:
                    print('LONGHORN_CLEANUP_HEARTBEAT owner='+Path(argv[-1]).name, flush=True); heartbeat=now
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
        if rc: raise ValueError('owner cleanup failed rc='+str(rc)+' log='+str(log))
    lines = log.read_text().splitlines()
    if any(FAILURE.match(line) for line in lines):
        raise ValueError('owner cleanup emitted a failure marker: '+str(log))
    if lines.count(expected) != 1:
        raise ValueError('owner cleanup lacks exactly one success marker '+expected+' log='+str(log))


@contextmanager
def relay(lab, scope):
    helper = {'v1':'longhorn-lab.py', 'v2':'longhorn-v2-install.py', 'target':'longhorn-v2-dr-target.py'}[scope]
    lab.relay_ensure()
    child = subprocess.Popen([sys.executable, str(BASE/'tests/helpers'/helper), 'relay-maintain'],
        stdin=subprocess.DEVNULL, pass_fds=(7,8,9))
    try:
        if child.poll() is not None: raise ValueError('cleanup relay exited before recovery')
        yield child.pid
        if child.poll() is not None: raise ValueError('cleanup relay exited during recovery')
    finally:
        if child.poll() is None: child.terminate()
        try: rc = child.wait(timeout=120)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=10); raise ValueError('cleanup relay did not terminate')
        lab.relay_cleanup()
        if rc: raise ValueError('cleanup relay returned nonzero')


def recover(action, work):
    log = work/(action.scope+'-'+action.name+'.log')
    if action.shell:
        run(['bash', str(BASE/'tests/helpers'/action.helper), action.mode], log, expected_marker(action))
    else:
        lab = fixture(action.scope); lab.require_lease(); lab.core_identity()
        with relay(lab, action.scope) as pid:
            if action.name == 'business':
                # Only plan's explicitly classified ordinary owner reaches here.
                lab.cleanup_business(relay_pid=pid, recover_recurring_selector=True)
                lab.cleanup_recurring_qualification(pid)
                lab.backup_store().cleanup()
            else:
                argv = [sys.executable, str(BASE/'tests/helpers'/action.helper), action.mode]
                # The DR coordinator reuses its original UID-bound cleanup;
                # its CLI owns both sides and intentionally has no relay-pid.
                if action.name != 'disaster-recovery': argv += ['--relay-pid', str(pid)]
                run(argv, log, expected_marker(action))


def verify(scopes):
    for scope, (before, _) in scopes.items():
        lab = fixture(scope); lab.require_lease(); lab.core_identity(); after = lab.owner()
        if any(after.get(key) != before.get(key) for key in ('run_id','cluster_uid','node_uids','namespace_uid','raw_disks')):
            raise ValueError('cleanup changed original core ownership: '+scope)
        if after.get('namespace_uid'): lab.installation_health()
        native = ('volumes.longhorn.io','engines.longhorn.io','replicas.longhorn.io',
            'snapshots.longhorn.io','backups.longhorn.io','backupvolumes.longhorn.io')
        volumes = [obj for kind in native for obj in lab.get(kind, namespace='longhorn-system')['items']] if after.get('namespace_uid') else []
        if (after.get('relays') or volumes or
                any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in lab.get('pv')['items']) or
                any(n['metadata']['name'].startswith('longhorn-business-') for n in lab.get('namespaces')['items'])):
            raise ValueError('cleanup retains an owned consumer or transport: '+scope)
        if scope in ('v1','v2'):
            store = lab.backup_store()
            if (store.directory.exists() or store.directory.is_symlink() or store.record.exists() or
                    store.record.is_symlink() or store.transport_listener() is not None):
                raise ValueError('cleanup retains a backup backend or transport: '+scope)
        if after.get('namespace_uid') and any(obj['metadata'].get('labels', {}).get('kubeauto.io/longhorn-backup-run')
                for kind in ('pods','secrets') for obj in lab.get(kind, namespace='longhorn-system')['items']):
            raise ValueError('cleanup retains a backup consumer or credential: '+scope)
        if scope == 'v2' and (lab.DIRECTORY/'longhorn-v2-cpu-isolation.json').exists():
            cpu = load('longhorn-v2-cpu-isolation.py'); row = cpu.receipt(lab)
            setting = lab.get('settings.longhorn.io', cpu.SETTING, cpu.NS)
            if (row.get('clean') is not True or not cpu.managers(lab, row, False) or
                    (lab.DIRECTORY/'config.yml').read_text() != row['original_config'] or
                    {'uid':setting['metadata']['uid'], 'value':setting['value']} != row['setting']):
                raise ValueError('CPU isolation recovered receipt differs from its actual baseline')
        print('LONGHORN_CLEANUP_BASELINE_VERIFIED scope='+scope, flush=True)


class ForeignCanary:
    """Independent UID-bound data fixture, never owned by cleanup's journals."""
    def __init__(self, lab, work, scope):
        self.lab = lab; self.record = work/('foreign-'+scope+'.json')

    @staticmethod
    def binding(obj):
        if not obj: return None
        meta = obj['metadata']
        return {'apiVersion':obj['apiVersion'], 'kind':obj['kind'],
            'metadata':{k:meta.get(k) for k in ('name','namespace','uid','labels','annotations')},
            **{k:obj.get(k) for k in ('spec','data','binaryData','immutable')}}

    def create(self):
        if self.record.exists() or self.record.is_symlink(): raise ValueError('previous foreign canary receipt')
        state = self.lab.owner(); namespace = 'longhorn-cleanup-canary-'+uuid.uuid4().hex[:16]
        row = {'run_id':state['run_id'], 'cluster_uid':state['cluster_uid'], 'namespace':namespace}
        self.lab.write_private(self.record, row)
        def create(obj):
            meta = obj['metadata']
            if self.lab.get(obj['kind'], meta['name'], meta.get('namespace','')):
                raise ValueError('foreign canary name collision')
            result = json.loads(self.lab.kubectl('create','-f','-','-o','json',stdin=json.dumps(obj)).stdout)
            if (result.get('kind') != obj['kind'] or result.get('metadata',{}).get('name') != meta['name'] or
                    result['metadata'].get('namespace','') != meta.get('namespace','') or
                    not result['metadata'].get('uid')):
                raise ValueError('foreign canary has no confirmed returned UID')
            return result
        ns = create({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}})
        row['namespace_binding'] = self.binding(ns); self.lab.write_private(self.record,row)
        row['defaults'] = self.lab.wait('cleanup-canary-namespace-defaults',60,lambda:self.lab.namespace_defaults(namespace))
        self.lab.write_private(self.record,row)
        cm = create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'foreign-data','namespace':namespace},
            'immutable':True,'data':{'payload':'independent cleanup canary '+uuid.uuid4().hex}})
        row['data_binding'] = self.binding(cm); self.lab.write_private(self.record,row)
        self.verify()

    def receipt(self):
        row = read(self.record); state = self.lab.owner()
        if any(row.get(k) != state.get(k) for k in ('run_id','cluster_uid')):
            raise ValueError('foreign canary belongs to another core')
        if not row.get('namespace_binding') or not row.get('data_binding') or not row.get('defaults'):
            raise ValueError('foreign canary creation has no complete original UID receipt')
        return row

    def verify(self):
        row = self.receipt(); namespace = row['namespace']
        ns = self.lab.get('namespace',namespace); cm = self.lab.get('configmap','foreign-data',namespace)
        if self.binding(ns) != row['namespace_binding'] or self.binding(cm) != row['data_binding']:
            raise ValueError('foreign canary UID or content changed')
        self.lab.check_business_graph({'namespace':namespace,'namespace_uid':ns['metadata']['uid'],
            'defaults':row['defaults'],'objects':{'ConfigMap/foreign-data':cm['metadata']['uid']}})
        return {'namespace_uid':ns['metadata']['uid'],'configmap_uid':cm['metadata']['uid'],
            'payload_sha256':hashlib.sha256(json.dumps(cm['data'],sort_keys=True).encode()).hexdigest()}

    def clean(self):
        self.verify(); row = self.receipt(); namespace = row['namespace']
        self.lab.remove(self.lab.get('configmap','foreign-data',namespace))
        self.lab.wait('cleanup-canary-data-removed',60,lambda:not self.lab.get('configmap','foreign-data',namespace))
        ns = self.lab.get('namespace',namespace)
        if self.binding(ns) != row['namespace_binding']: raise ValueError('foreign canary namespace changed before cleanup')
        self.lab.check_business_graph({'namespace':namespace,'namespace_uid':ns['metadata']['uid'],
            'defaults':row['defaults'],'objects':{}})
        self.lab.remove(ns)
        self.lab.wait('cleanup-canary-namespace-removed',180,lambda:not self.lab.get('namespace',namespace))
        row['clean'] = True; self.lab.write_private(self.record,row)


def fixture_child(scope, case):
    """Create only an independent owner CRD; leave its journal to public cleanup."""
    if os.environ.get('LONGHORN_CLEANUP_FIXTURE_CHILD') != 'yes': raise ValueError('fixture child requires its qualifier')
    lab = fixture(scope); lab.require_lease(); lab.core_identity(); lab.installation_health()
    helper = load('longhorn-ownership.py').PreinstallProbe(lab)
    row = {key:lab.owner()[key] for key in ('run_id','cluster_uid')}
    row.update(scenario='crd',desired=helper.desired('crd'))
    if helper.path.exists() or helper.path.is_symlink() or helper.current(row):
        raise ValueError('cleanup fixture refuses an existing owner journal or CRD')
    lab.write_private(helper.path,row)
    obj = json.loads(lab.kubectl('create','-f','-','-o','json',stdin=json.dumps(row['desired'])).stdout)
    if (obj.get('kind') != 'CustomResourceDefinition' or not obj.get('metadata',{}).get('uid') or
            obj['metadata'].get('name') != row['desired']['metadata']['name']):
        raise ValueError('cleanup fixture lacks its returned original CRD UID')
    row['created'] = helper.binding(obj); lab.write_private(helper.path,row)
    lab.wait('cleanup-fixture-established',60,lambda:any(c.get('type')=='Established' and c.get('status')=='True'
        for c in (helper.current(row) or {}).get('status',{}).get('conditions',[])))
    print('LONGHORN_CLEANUP_FIXTURE_READY',flush=True)
    if case == 'normal': return 0
    if case == 'failure': return 86
    parent = os.getppid()
    while os.getppid() == parent and parent != 1: time.sleep(.2)
    raise ValueError('cleanup fixture lost its original qualifier')


def prepare_fixture(lab, scope, case, work):
    log = work/(scope+'-'+case+'-fixture.log')
    helper = load('longhorn-ownership.py').PreinstallProbe(lab)
    argv = [sys.executable,str(BASE/'tests/helpers/longhorn-cleanup.py'),'--fixture-child',case,'--scope',scope]
    with log.open('xb') as output:
        child = subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
            start_new_session=True,pass_fds=(7,8,9),env=dict(os.environ,LONGHORN_CLEANUP_FIXTURE_CHILD='yes'))
        try:
            def ready():
                lines = log.read_text().splitlines()
                if any(FAILURE.match(line) for line in lines): raise ValueError('cleanup fixture failed before its ready boundary')
                if 'LONGHORN_CLEANUP_FIXTURE_READY' in lines: return True
                if child.poll() is not None: raise ValueError('cleanup fixture exited before creating its original journal')
                return False
            lab.wait('cleanup-fixture-ready',90,ready)
            row = helper.receipt()
            if not row.get('created') or helper.binding(helper.current(row)) != row['created']:
                raise ValueError('cleanup fixture original journal or CRD changed')
            if case == 'interruption':
                # Only this unreaped, directly owned, create-only child. The
                # original owner TERM unwind would consume its own journal.
                if child.poll() is not None: raise ValueError('cleanup interruption fixture exited before its owned signal')
                child.kill()
            lab.wait('cleanup-fixture-exit',120,lambda:child.poll() is not None)
            expected = {'normal':0,'failure':86,'interruption':-9}[case]
            if child.returncode != expected: raise ValueError('cleanup fixture unexpected exit state')
            if helper.receipt() != row or helper.binding(helper.current(row)) != row['created']:
                raise ValueError('cleanup fixture did not retain its original recoverable journal')
            lab.write_private(work/(scope+'-'+case+'-fixture.json'),{
                'case':case,'scope':scope,'exit':child.returncode,'journal':row,
                'log_sha256':hashlib.sha256(log.read_bytes()).hexdigest()})
            print('LONGHORN_CLEANUP_FIXTURE_OBSERVED scope='+scope+' case='+case+' rc='+str(expected)+
                ' uid='+row['created']['uid'],flush=True)
            return row
        finally:
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=120)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=10)


def public_cleanup(work, scope, case):
    """Use the fixed live dispatch under the already-held authoritative lease."""
    prefix = work/(scope+'-'+case+'-cleanup'); log = prefix.with_suffix('.log')
    run(['bash',str(BASE/'tests/helpers/run-durable-gate.sh'),str(prefix),'LONGHORN_CLEANUP_STAGE_EXIT',
        'bash',str(BASE/'tests/helpers/longhorn-live.sh'),'cleanup'],log,'LONGHORN_CLEANUP_PASS')
    lines = log.read_text().splitlines()
    for suffix in ('.exit','.finalized'):
        path = prefix.with_suffix(suffix); info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or
                path.read_text().strip() != '0'):
            raise ValueError('public cleanup did not durably finalize successfully')
    for marker in ('LONGHORN_CLEANUP_SCOPED_CLEAN_PASS','LONGHORN_CLEANUP_STAGE_EXIT rc=0',
            'LONGHORN_CLEANUP_OWNER_START scope='+scope+' owner=business'):
        if lines.count(marker) != 1: raise ValueError('public cleanup did not prove its journal owner: '+marker)


def qualify(scopes, actions, work):
    if actions: raise ValueError('cleanup qualification requires no active scenario owner')
    if any(scope not in scopes or not scopes[scope][0].get('namespace_uid') for scope in ('v1','v2')):
        raise ValueError('cleanup qualification requires both installed V1 and V2 original scopes')
    verify(scopes); protected = {}; canaries = {}; success = False; active_scope = None
    try:
        for scope in ('v1','v2'):
            lab = fixture(scope); lab.require_lease(); lab.core_identity()
            if any(n['metadata']['name'].startswith('longhorn-cleanup-canary-') for n in lab.get('namespaces')['items']):
                raise ValueError('previous foreign canary retained; recover using its private original UID receipt')
            protected[scope] = {'config':(lab.DIRECTORY/'config.yml').read_bytes(),'shared':lab.shared_snapshot_identity()}
            canary = ForeignCanary(lab,work,scope); canaries[scope] = canary; canary.create()
            foreign = canary.verify()
            for case in ('normal','failure','interruption'):
                active_scope = scope
                row = prepare_fixture(lab,scope,case,work)
                public_cleanup(work,scope,case)
                owner = load('longhorn-ownership.py').PreinstallProbe(lab)
                if owner.path.exists() or owner.path.is_symlink() or owner.current(row):
                    raise ValueError('public cleanup left the original fixture journal or CRD')
                if ((lab.DIRECTORY/'config.yml').read_bytes() != protected[scope]['config'] or
                        lab.shared_snapshot_identity() != protected[scope]['shared'] or canary.verify() != foreign):
                    raise ValueError('cleanup changed protected product input/shared/foreign identity')
                print('LONGHORN_CLEANUP_CASE_PASS '+json.dumps({'scope':scope,'case':case,'foreign':foreign},sort_keys=True),flush=True)
        active_scope = None
        for canary in canaries.values(): canary.clean()
        _, remaining = plan(BASE)
        if remaining: raise ValueError('qualification retains a scenario journal')
        verify(scopes); success = True
        print('LONGHORN_CLEANUP_QUALIFICATION_SCOPED_CLEAN_PASS',flush=True)
    finally:
        if not success:
            # Restore only a proven ordinary owner through the same public
            # route. An uncertain create/foreign replacement remains refused.
            if active_scope:
                try:
                    _, recovery = plan(BASE)
                    if recovery:
                        if len(recovery) != 1 or recovery[0].scope != active_scope or recovery[0].name != 'business':
                            raise ValueError('qualification failure overlaps another scenario owner')
                        public_cleanup(work,active_scope,'qualification-recovery')
                except Exception as error:
                    print('LONGHORN_CLEANUP_QUALIFICATION_RECOVERY_REFUSED reason='+str(error),flush=True)
            for scope, canary in canaries.items():
                try:
                    if not canary.receipt().get('clean'): canary.clean()
                except Exception as error:
                    print('LONGHORN_CLEANUP_QUALIFICATION_CANARY_RETAINED scope='+scope+' reason='+str(error),flush=True)
            print('LONGHORN_CLEANUP_QUALIFICATION_RETAINED diagnostics='+str(work),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan',action='store_true');mode.add_argument('--qualify',action='store_true')
    mode.add_argument('--fixture-child',choices=('normal','failure','interruption'))
    parser.add_argument('--scope',choices=('v1','v2'))
    args = parser.parse_args()
    if args.fixture_child:
        if not args.scope: parser.error('fixture child needs its fixed scope')
        load('longhorn-lab.py').require_lease()
        raise SystemExit(fixture_child(args.scope,args.fixture_child))
    if args.scope: parser.error('scope is restricted to the fixed qualification child')
    scopes, actions = plan(BASE)
    if args.plan:
        print(json.dumps([{'scope':r.scope,'owner':r.name,'helper':r.helper,'mode':r.mode} for r in actions], sort_keys=True)); return
    if os.environ.get('LONGHORN_PARENT_LEASE_HELD') != 'yes': raise ValueError('missing fixed parent lease')
    load('longhorn-lab.py').require_lease()
    work = Path(os.environ['LONGHORN_CLEANUP_WORK'])
    if (work.is_symlink() or not work.is_dir() or work.stat().st_uid != os.geteuid() or
            work.stat().st_mode & 0o077):
        raise ValueError('cleanup requires a private durable work directory')
    if args.qualify:
        qualify(scopes,actions,work); return
    for action in actions:
        print('LONGHORN_CLEANUP_OWNER_START scope='+action.scope+' owner='+action.name, flush=True)
        recover(action, work)
        print('LONGHORN_CLEANUP_OWNER_PASS scope='+action.scope+' owner='+action.name, flush=True)
    _, remaining = plan(BASE)
    if remaining: raise ValueError('owner recovery left active journals')
    verify(scopes)
    print('LONGHORN_CLEANUP_SCOPED_CLEAN_PASS', flush=True)


if __name__ == '__main__':
    def interrupted(number, frame): raise SystemExit(128+number)
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGINT, interrupted)
    try: main()
    except Exception as error:
        print('LONGHORN_CLEANUP_FAIL reason='+str(error), file=sys.stderr); sys.exit(1)
