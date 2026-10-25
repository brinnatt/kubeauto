#!/usr/bin/env python3
"""Read-only context and terminal evidence checks for fixed parallel branches."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat


MARKERS = {
    'idempotence': ('LONGHORN_IDEMPOTENCE_SCOPED_CLEAN_PASS', 'LONGHORN_IDEMPOTENCE_PASS',
                    'LONGHORN_IDEMPOTENCE_EXIT rc=0'),
    'security': ('LONGHORN_SECURITY_SCOPED_CLEAN_PASS', 'LONGHORN_SECURITY_PASS',
                 'LONGHORN_SECURITY_EXIT rc=0'),
    'v1': ('LONGHORN_IDEMPOTENCE_SCOPED_CLEAN_PASS', 'LONGHORN_IDEMPOTENCE_PASS',
           'LONGHORN_IDEMPOTENCE_EXIT rc=0',
           'LONGHORN_SECURITY_SCOPED_CLEAN_PASS', 'LONGHORN_SECURITY_PASS',
           'LONGHORN_SECURITY_EXIT rc=0',
           'LONGHORN_PARALLEL_V1_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0'),
    'v2': ('LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_SCOPED_CLEAN_PASS',
           'LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_PASS',
           'LONGHORN_PARALLEL_V2_EXIT rc=0'),
    'v2-linked': ('LONGHORN_V2_LINKED_CLONE_QUALIFICATION_SCOPED_CLEAN_PASS',
                  'LONGHORN_V2_LINKED_CLONE_QUALIFICATION_PASS',
                  'LONGHORN_PARALLEL_V2_EXIT rc=0'),
    'v1-adopt': ('LONGHORN_V1_IM_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS',
                 'LONGHORN_V1_IM_ADOPT_QUALIFICATION_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0'),
    'v2-adopt': ('LONGHORN_V2_IM_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS',
                 'LONGHORN_V2_IM_ADOPT_QUALIFICATION_PASS', 'LONGHORN_PARALLEL_V2_EXIT rc=0'),
}
# Source and DR target are independent clusters; layout names remain compatible.
for profile, prefix in (('source', 'v1'), ('target', 'v2')):
    tag = 'V2_'+('DR_TARGET_' if profile == 'target' else '')
    for operation, suffix in (('reboot', 'RESOURCES_REBOOT_RECOVERY'),
                              ('adopt', 'IM_ADOPT_QUALIFICATION')):
        marker = 'LONGHORN_'+tag+suffix
        MARKERS[profile+'-'+operation] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS',
                                         'LONGHORN_PARALLEL_'+prefix.upper()+'_EXIT rc=0')
V1_BACKUP_MODULES = ('platform', 'security', 'backup', 'backup-refusal', 'recurring', 'encryption', 'uninstall')
for module in V1_BACKUP_MODULES:
    marker = 'LONGHORN_' + module.upper().replace('-', '_')
    MARKERS[module] = (marker + '_SCOPED_CLEAN_PASS', marker + '_PASS', marker + '_EXIT rc=0')
MARKERS['v1-backups'] = tuple(marker for module in V1_BACKUP_MODULES for marker in MARKERS[module]) + (
    'LONGHORN_PARALLEL_V1_BACKUPS_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0')
# Both fixed parent batches retain the same v1/v2 durable file layout.
BRANCH_PREFIXES = {'v1-backups': 'v1', 'v2-linked': 'v2', 'v1-adopt': 'v1', 'v2-adopt': 'v2'}
BRANCH_PREFIXES.update({profile+'-'+op: prefix for profile, prefix in
                        (('source', 'v1'), ('target', 'v2')) for op in ('reboot', 'adopt')})
for profile, tag, prefix in (('source', 'V2_', 'v1'), ('target', 'V2_DR_TARGET_', 'v2')):
    marker = 'LONGHORN_'+tag+'POST_REBOOT_ENVIRONMENT_RECOVERY'
    MARKERS[profile+'-environment'] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS',
                                      'LONGHORN_PARALLEL_'+prefix.upper()+'_EXIT rc=0')
    BRANCH_PREFIXES[profile+'-environment'] = prefix
    marker = 'LONGHORN_'+tag+'IM_REVISION_2_ADOPT_QUALIFICATION'
    MARKERS[profile+'-revision-2-adopt'] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS',
                                         'LONGHORN_PARALLEL_'+prefix.upper()+'_EXIT rc=0')
    BRANCH_PREFIXES[profile+'-revision-2-adopt'] = prefix
    marker = 'LONGHORN_'+tag+'IM_REVISION_3_ADOPT_QUALIFICATION'
    MARKERS[profile+'-revision-3-adopt'] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS',
                                         'LONGHORN_PARALLEL_'+prefix.upper()+'_EXIT rc=0')
    BRANCH_PREFIXES[profile+'-revision-3-adopt'] = prefix
MARKERS['v1-platform'] = ('LONGHORN_PLATFORM_SCOPED_CLEAN_PASS', 'LONGHORN_PLATFORM_PASS',
                         'LONGHORN_PARALLEL_V1_EXIT rc=0')
BRANCH_PREFIXES['v1-platform'] = 'v1'
# The nested source/target supervisor has its own child exit markers. Its
# outer durable fences and unique terminal markers establish this scope.
MARKERS['v2-environment'] = ('LONGHORN_V2_SOURCE_TARGET_ENVIRONMENT_RECOVERY_SCOPED_CLEAN_PASS',
                            'LONGHORN_V2_SOURCE_TARGET_ENVIRONMENT_RECOVERY_PASS')
BRANCH_PREFIXES['v2-environment'] = 'v2'
MARKERS['v1-backups-remaining'] = tuple(marker for module in V1_BACKUP_MODULES[1:] for marker in MARKERS[module]) + (
    'LONGHORN_PARALLEL_V1_BACKUPS_REMAINING_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0')
MARKERS['v2-linked-acceptance'] = ('LONGHORN_V2_LINKED_ACCEPTANCE_SCOPED_CLEAN_PASS',
                                    'LONGHORN_V2_LINKED_ACCEPTANCE_PASS')
for mode, suffix in (('linked-clone', 'QUALIFICATION'), ('linked-lifecycle', 'QUALIFICATION')):
    marker = 'LONGHORN_V2_'+mode.upper().replace('-', '_')+'_'+suffix
    MARKERS[mode] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS', marker+'_EXIT rc=0')
BRANCH_PREFIXES.update({'v1-backups-remaining': 'v1', 'v2-linked-acceptance': 'v2'})
MARKERS['v1-propagation'] = ('LONGHORN_PLATFORM_PROPAGATION_QUALIFICATION_SCOPED_CLEAN_PASS',
                            'LONGHORN_PLATFORM_PROPAGATION_QUALIFICATION_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0')
MARKERS['v2-adoption'] = ('LONGHORN_V2_SOURCE_TARGET_IM_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS',
                         'LONGHORN_V2_SOURCE_TARGET_IM_ADOPT_QUALIFICATION_PASS')
BRANCH_PREFIXES.update({'v1-propagation': 'v1', 'v2-adoption': 'v2'})
MARKERS['failure'] = ('LONGHORN_V2_FAILURE_SCOPED_CLEAN_PASS', 'LONGHORN_V2_FAILURE_PASS',
                      'LONGHORN_V2_FAILURE_EXIT rc=0')
revision = 'LONGHORN_V1_IM_REVISION_2_ADOPT_QUALIFICATION'
MARKERS['v1-im-revision-2-adopt-qualification'] = (revision+'_SCOPED_CLEAN_PASS', revision+'_PASS', revision+'_EXIT rc=0')
MARKERS['v1-post-revision-backups'] = tuple(marker for module in (
    'v1-im-revision-2-adopt-qualification', 'backup-refusal', 'recurring', 'encryption', 'uninstall')
    for marker in MARKERS[module]) + ('LONGHORN_PARALLEL_V1_POST_REVISION_BACKUPS_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0')
BRANCH_PREFIXES['v1-post-revision-backups'] = 'v1'
MARKERS['v2-failure-acceptance'] = ('LONGHORN_V2_FAILURE_SCOPED_CLEAN_PASS', 'LONGHORN_V2_FAILURE_PASS',
                                  'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-failure-acceptance'] = 'v2'
MARKERS['v2-linked-failure-acceptance'] = ('LONGHORN_V2_LINKED_FAILURE_ACCEPTANCE_SCOPED_CLEAN_PASS',
                                        'LONGHORN_V2_LINKED_FAILURE_ACCEPTANCE_PASS')
BRANCH_PREFIXES['v2-linked-failure-acceptance'] = 'v2'
MARKERS['v2-runtime-resources'] = ('LONGHORN_V2_RUNTIME_RESOURCES_GATE_SCOPED_CLEAN_PASS',
    'LONGHORN_V2_RUNTIME_RESOURCES_PASS', 'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-runtime-resources'] = 'v2'
MARKERS['v1-stability'] = ('LONGHORN_STABILITY_SCOPED_CLEAN_PASS', 'LONGHORN_STABILITY_PASS',
                         'LONGHORN_PARALLEL_V1_EXIT rc=0')
MARKERS['v2-observability'] = ('LONGHORN_V2_OBSERVABILITY_PERFORMANCE_SCOPED_CLEAN_PASS',
                             'LONGHORN_V2_OBSERVABILITY_PERFORMANCE_PASS', 'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES.update({'v1-stability': 'v1', 'v2-observability': 'v2'})
MARKERS['v1-ownership'] = ('LONGHORN_OWNERSHIP_SCOPED_CLEAN_PASS', 'LONGHORN_OWNERSHIP_PASS',
                            'LONGHORN_PARALLEL_V1_EXIT rc=0')
BRANCH_PREFIXES['v1-ownership'] = 'v1'
MARKERS['incomplete-clone'] = ('LONGHORN_V2_INCOMPLETE_CLONE_QUALIFICATION_SCOPED_CLEAN_PASS',
                               'LONGHORN_V2_INCOMPLETE_CLONE_QUALIFICATION_PASS',
                               'LONGHORN_V2_INCOMPLETE_CLONE_QUALIFICATION_EXIT rc=0')
V1_OPERATION_MODULES = ('maintenance', 'capacity', 'integrity', 'disk-lifecycle',
                        'node-failure', 'network-failure', 'disk-failure', 'last-replica', 'middleware')
for module in V1_OPERATION_MODULES:
    marker = 'LONGHORN_'+module.upper().replace('-', '_')
    terminal = marker+'_GATE' if module in ('disk-lifecycle', 'node-failure', 'last-replica') else marker
    MARKERS[module] = (terminal+'_SCOPED_CLEAN_PASS', terminal+'_PASS', marker+'_EXIT rc=0')
MARKERS['v1-operations'] = tuple(marker for module in V1_OPERATION_MODULES for marker in MARKERS[module]) + (
    'LONGHORN_PARALLEL_V1_OPERATIONS_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0')
BRANCH_PREFIXES['v1-operations'] = 'v1'
MARKERS['capacity-cleanup'] = ('LONGHORN_CAPACITY_CLEANUP_SCOPED_CLEAN_PASS',
    'LONGHORN_CAPACITY_CLEANUP_PASS', 'LONGHORN_CAPACITY_CLEANUP_EXIT rc=0')
MARKERS['node-failure-cleanup'] = ('LONGHORN_NODE_FAILURE_CLEANUP_SCOPED_CLEAN_PASS',
    'LONGHORN_NODE_FAILURE_CLEANUP_PASS', 'LONGHORN_NODE_FAILURE_CLEANUP_EXIT rc=0')
V1_OPERATION_REMAINING_MODULES = V1_OPERATION_MODULES[4:]
MARKERS['v1-operations-remaining'] = tuple(marker for module in V1_OPERATION_REMAINING_MODULES
    for marker in MARKERS[module]) + ('LONGHORN_PARALLEL_V1_OPERATIONS_REMAINING_PASS',
                                     'LONGHORN_PARALLEL_V1_EXIT rc=0')
BRANCH_PREFIXES['v1-operations-remaining'] = 'v1'
MARKERS['v2-observability-monitoring'] = ('LONGHORN_V2_OBSERVABILITY_MONITORING_SCOPED_CLEAN_PASS',
    'LONGHORN_V2_OBSERVABILITY_MONITORING_PASS', 'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-observability-monitoring'] = 'v2'
MARKERS['v2-platform-install'] = ('LONGHORN_V2_PLATFORM_INSTALL_BATCH_SCOPED_CLEAN_PASS',
                                 'LONGHORN_V2_PLATFORM_INSTALL_BATCH_PASS',
                                 'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-platform-install'] = 'v2'
MARKERS['v2-failure-remaining'] = ('LONGHORN_V2_FAILURE_REMAINING_SCOPED_CLEAN_PASS',
                                 'LONGHORN_V2_FAILURE_REMAINING_PASS',
                                 'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-failure-remaining'] = 'v2'
MARKERS['v2-middleware'] = ('LONGHORN_V2_MIDDLEWARE_SCOPED_CLEAN_PASS',
                             'LONGHORN_V2_MIDDLEWARE_PASS',
                             'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-middleware'] = 'v2'
MARKERS['v2-interrupt-qualification'] = ('LONGHORN_V2_INTERRUPT_QUALIFICATION_SCOPED_CLEAN_PASS',
                                         'LONGHORN_V2_INTERRUPT_QUALIFICATION_PASS',
                                         'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-interrupt-qualification'] = 'v2'
for module, marker in (
        ('system-restore', 'LONGHORN_SYSTEM_RESTORE_GATE'),
        ('disaster-recovery', 'LONGHORN_DISASTER_RECOVERY'),
        ('v1-upgrade-lifecycle', 'LONGHORN_V1_UPGRADE_GATE'),
        ('observability', 'LONGHORN_OBSERVABILITY')):
    MARKERS[module] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS',
                       'LONGHORN_PARALLEL_V1_EXIT rc=0')
    BRANCH_PREFIXES[module] = 'v1'
MARKERS['observability-support'] = ('LONGHORN_OBSERVABILITY_SUPPORT_SCOPED_CLEAN_PASS',
                                    'LONGHORN_OBSERVABILITY_SUPPORT_PASS',
                                    'LONGHORN_PARALLEL_V1_EXIT rc=0')
BRANCH_PREFIXES['observability-support'] = 'v1'
for branch, prefix, marker in (
        ('v1-recovery-tail', 'v1', 'LONGHORN_PARALLEL_V1_RECOVERY_TAIL'),
        ('v1-dr-tail', 'v1', 'LONGHORN_PARALLEL_V1_DR_TAIL'),
        ('v1-upgrade-tail', 'v1', 'LONGHORN_PARALLEL_V1_UPGRADE_TAIL'),
        ('v2-install-tail', 'v2', 'LONGHORN_V2_INSTALL_TAIL_BATCH'),
        ('v2-ready-tail', 'v2', 'LONGHORN_PARALLEL_V2_READY_TAIL')):
    MARKERS[branch] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS')
    BRANCH_PREFIXES[branch] = prefix
MARKERS['v2-install-ready-tail'] = ('LONGHORN_PARALLEL_V2_INSTALL_READY_TAIL_SCOPED_CLEAN_PASS',
    'LONGHORN_PARALLEL_V2_INSTALL_READY_TAIL_PASS')
BRANCH_PREFIXES['v2-install-ready-tail'] = 'v2'
MARKERS['v2-maintenance-tail'] = ('LONGHORN_PARALLEL_V2_MAINTENANCE_TAIL_SCOPED_CLEAN_PASS',
    'LONGHORN_PARALLEL_V2_MAINTENANCE_TAIL_PASS')
BRANCH_PREFIXES['v2-maintenance-tail'] = 'v2'
MARKERS['v2-maintenance-drain-qualification'] = ('LONGHORN_V2_MAINTENANCE_DRAIN_BUSINESS_PASS',
    'LONGHORN_V2_MAINTENANCE_DRAIN_FIXTURES_CLEAN_PASS',
    'LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_SCOPED_CLEAN_PASS',
    'LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_PASS', 'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-maintenance-drain-qualification'] = 'v2'

MARKERS['system-restore-prepared-recovery'] = MARKERS['system-restore']
BRANCH_PREFIXES['system-restore-prepared-recovery'] = 'v1'
# These sequential wrappers already verify each original durable scope.
# Nested exit markers occur more than once; only wrapper terminals are unique.
for branch, prefix, marker in (
        ('v1-ownership-operations', 'v1', 'LONGHORN_PARALLEL_V1_OWNERSHIP_OPERATIONS'),
        ('v2-failure-platform', 'v2', 'LONGHORN_PARALLEL_V2_FAILURE_PLATFORM')):
    MARKERS[branch] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS')
    BRANCH_PREFIXES[branch] = prefix
# Exact independent protection branches; V2 target consumers remain serial.
V1_DATA_PROTECTION_MODULES = ('backup', 'backup-refusal', 'recurring')
MARKERS['v1-data-protection'] = tuple(marker for module in V1_DATA_PROTECTION_MODULES
    for marker in MARKERS[module]) + ('LONGHORN_PARALLEL_V1_DATA_PROTECTION_PASS',
                                      'LONGHORN_PARALLEL_V1_EXIT rc=0')
MARKERS['v2-data-protection'] = ('LONGHORN_V2_DATA_PROTECTION_SCOPED_CLEAN_PASS',
    'LONGHORN_V2_DATA_PROTECTION_PASS', 'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES.update({'v1-data-protection': 'v1', 'v2-data-protection': 'v2'})
# Fixed continuation branches consume only the original owning scopes.
for module in ('ownership-controller-refusal-qualification', 'ownership-request-refusal-qualification'):
    marker = 'LONGHORN_'+module.upper().replace('-', '_')
    MARKERS[module] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS',
                       'LONGHORN_PARALLEL_V1_EXIT rc=0')
    BRANCH_PREFIXES[module] = 'v1'
MARKERS['maintenance-cleanup'] = ('LONGHORN_MAINTENANCE_CLEANUP_SCOPED_CLEAN_PASS',
    'LONGHORN_MAINTENANCE_CLEANUP_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0')
BRANCH_PREFIXES['maintenance-cleanup'] = 'v1'
MARKERS['v2-failure-cleanup'] = ('LONGHORN_V2_FAILURE_CLEANUP_SCOPED_CLEAN_PASS',
    'LONGHORN_V2_FAILURE_CLEANUP_PASS', 'LONGHORN_PARALLEL_V2_EXIT rc=0')
BRANCH_PREFIXES['v2-failure-cleanup'] = 'v2'
for case in ('remote-spdk', 'replica-network', 'worker-reboot', 'single-disk', 'rebuild-qos', 'all-replicas'):
    module = 'v2-failure-'+case
    marker = 'LONGHORN_'+module.upper().replace('-', '_')
    MARKERS[module] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS',
        'LONGHORN_V2_FAILURE_STAGE_PASS case='+case+' durable_rc=0 finalized=0',
        'LONGHORN_PARALLEL_V2_EXIT rc=0')
    BRANCH_PREFIXES[module] = 'v2'
for branch, prefix, marker in (
        ('v1-ownership-tail-operations', 'v1', 'LONGHORN_PARALLEL_V1_OWNERSHIP_TAIL_OPERATIONS'),
        ('v2-failure-tail-platform', 'v2', 'LONGHORN_PARALLEL_V2_FAILURE_TAIL_PLATFORM'),
        ('v1-operations-recovery', 'v1', 'LONGHORN_PARALLEL_V1_OPERATIONS_RECOVERY'),
        ('v2-worker-tail-platform', 'v2', 'LONGHORN_PARALLEL_V2_WORKER_TAIL_PLATFORM'),
        ('v1-operations-cleanup-recovery', 'v1', 'LONGHORN_PARALLEL_V1_OPERATIONS_CLEANUP_RECOVERY'),
        ('v1-operations-remaining-recovery', 'v1', 'LONGHORN_PARALLEL_V1_OPERATIONS_REMAINING_RECOVERY'),
        ('v2-disk-tail-platform', 'v2', 'LONGHORN_PARALLEL_V2_DISK_TAIL_PLATFORM')):
    MARKERS[branch] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS')
    BRANCH_PREFIXES[branch] = prefix
MARKERS['observability-image-cache'] = ('LONGHORN_OBSERVABILITY_IMAGE_CACHE_SCOPED_CLEAN_PASS',
    'LONGHORN_OBSERVABILITY_IMAGE_CACHE_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0')
MARKERS['observability-cleanup'] = ('LONGHORN_OBSERVABILITY_SCOPED_CLEAN_PASS',
    'LONGHORN_OBSERVABILITY_CLEANUP_PASS', 'LONGHORN_PARALLEL_V1_EXIT rc=0')
for branch in ('observability-image-cache', 'observability-cleanup'):
    BRANCH_PREFIXES[branch] = 'v1'
for branch, prefix, marker in (
        ('v1-monitoring-tail', 'v1', 'LONGHORN_PARALLEL_V1_MONITORING_TAIL'),
        ('v2-resource-tail', 'v2', 'LONGHORN_PARALLEL_V2_RESOURCE_TAIL')):
    MARKERS[branch] = (marker+'_SCOPED_CLEAN_PASS', marker+'_PASS')
    BRANCH_PREFIXES[branch] = prefix
FAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)')


def context(path=Path('/root/.kube/config')):
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('unsafe shared kubeconfig path')
    if not path.exists():
        return {'exists': False}
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('shared kubeconfig is not a regular file')
    return {'exists': True, 'uid': info.st_uid, 'mode': stat.S_IMODE(info.st_mode),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
            info.st_mode & 0o077):
        raise ValueError('parallel evidence must be an owner-private regular file')


def verify_context(record, path=Path('/root/.kube/config')):
    private_file(record)
    if json.loads(record.read_text()) != context(path):
        raise ValueError('shared kubeconfig changed during explicit-context branch')


def verify_branch(directory, branch):
    if branch not in MARKERS or directory.is_symlink():
        raise ValueError('unknown or unsafe parallel branch')
    prefix = directory / BRANCH_PREFIXES.get(branch, branch)
    for suffix in ('.exit', '.finalized'):
        path = prefix.with_suffix(suffix)
        private_file(path)
        if path.read_text().strip() != '0':
            raise ValueError('parallel branch did not finalize successfully: ' + branch)
    log = prefix.with_suffix('.log')
    private_file(log)
    lines = log.read_text().splitlines()
    if any(FAILURE.match(line) for line in lines):
        raise ValueError('parallel branch contains failure markers: ' + branch)
    if any(lines.count(marker) != 1 for marker in MARKERS[branch]):
        raise ValueError('parallel branch terminal markers differ: ' + branch)
    if branch in ('v1-operations-remaining', 'v1-operations-remaining-recovery'):
        stage = 'LONGHORN_PARALLEL_V1_OPERATIONS_REMAINING_STAGE_'
        cleanup = (stage+'START stage=node-failure-cleanup', *MARKERS['node-failure-cleanup'],
                   stage+'PASS stage=node-failure-cleanup durable_rc=0 finalized=0')
        observed = [line for line in lines if line.startswith('LONGHORN_NODE_FAILURE_CLEANUP') or
                    line.startswith(stage) and 'stage=node-failure-cleanup' in line]
        if observed:
            start = stage+'START stage=node-failure'
            if (observed != list(cleanup) or any(lines.count(marker) != 1 for marker in cleanup) or
                    lines.count(start) != 1 or lines.index(cleanup[-1]) >= lines.index(start)):
                raise ValueError('remaining node-failure cleanup prefix is incomplete or out of order')
    return {'branch': branch, 'rc': 0, 'failure_markers': 0,
            'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('context-fingerprint')
    check = sub.add_parser('context-verify')
    check.add_argument('--record', type=Path, required=True)
    branch = sub.add_parser('verify-branch')
    branch.add_argument('--directory', type=Path, required=True)
    branch.add_argument('--branch', choices=MARKERS, required=True)
    args = parser.parse_args()
    if args.command == 'context-fingerprint':
        print(json.dumps(context(), sort_keys=True))
    elif args.command == 'context-verify':
        verify_context(args.record)
        print('LONGHORN_PARALLEL_SHARED_CONTEXT_UNCHANGED')
    else:
        print('LONGHORN_PARALLEL_BRANCH_VERIFIED ' +
              json.dumps(verify_branch(args.directory, args.branch), sort_keys=True))


if __name__ == '__main__':
    main()
