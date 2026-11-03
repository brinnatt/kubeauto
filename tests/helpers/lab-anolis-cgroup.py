#!/usr/bin/env python3
"""Temporary RT/cgroup qualification fixture, exclusively for Anolis lab 141.

Linux cgroup-v2 CPU requires RT tasks at the root when RT_GROUP_SCHED=y.
No scheduling policy or persistent service configuration is changed. Restore
requires a clean node and a reboot, which recreates the original service tree.
https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu
"""
import fcntl
import json
import os
import re
from pathlib import Path
import subprocess
import sys

PROC = Path('/proc')
CGROUP = Path('/sys/fs/cgroup')
JOURNAL = Path('/var/tmp/kubeauto-anolis-rt-fixture.json')


def boot_id():
    return (PROC / 'sys/kernel/random/boot_id').read_text().strip()


def process_identity(directory):
    # comm can contain whitespace and parentheses; starttime is stat field 22.
    return directory.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19]


def realtime_groups():
    records = []
    for directory in PROC.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            # sched_getscheduler returns policy OR SCHED_RESET_ON_FORK. Desktop
            # audio/rtkit commonly use that flag; it does not make them non-RT.
            is_rt = any((os.sched_getscheduler(int(task.name)) & ~os.SCHED_RESET_ON_FORK)
                        in (os.SCHED_FIFO, os.SCHED_RR)
                        for task in (directory / 'task').iterdir())
            if not is_rt:
                continue
            entries = directory.joinpath('cgroup').read_text().splitlines()
            group = next(line[3:] for line in entries if line.startswith('0::'))
            if group == '/':
                continue
            records.append({'pid': int(directory.name), 'starttime': process_identity(directory),
                            'cgroup': group})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return records


def require_idle():
    for unit in ('kubelet', 'containerd', 'docker', 'etcd'):
        state = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True,
                               timeout=10, check=False).stdout.strip()
        if state not in ('inactive', 'unknown'):
            raise RuntimeError('active or failed node service: ' + unit)
    for directory in PROC.iterdir():
        if directory.name.isdigit():
            try:
                groups = directory.joinpath('cgroup').read_text()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if 'kubepods' in groups or 'podruntime' in groups:
                raise RuntimeError('Pod process remains on node')


def prepare(run_id):
    require_idle()
    if JOURNAL.exists() or JOURNAL.is_symlink():
        raise RuntimeError('existing RT fixture journal; restore it before reuse')
    if not (CGROUP / 'cgroup.controllers').is_file():
        raise RuntimeError('requires unified cgroup v2')
    if 'cpu' not in (CGROUP / 'cgroup.controllers').read_text().split():
        raise RuntimeError('kernel has no CPU controller')
    records = realtime_groups()
    journal = {'schema': 1, 'host': '192.168.47.141', 'run_id': run_id, 'boot_id': boot_id(),
               'original_subtree': (CGROUP / 'cgroup.subtree_control').read_text().split(),
               'rt_processes': records}
    descriptor = os.open(JOURNAL, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(journal, stream)
        stream.flush()
        os.fsync(stream.fileno())
    for row in records:
        directory = PROC / str(row['pid'])
        try:
            if process_identity(directory) != row['starttime']:
                raise RuntimeError('RT process identity changed; restore before retry')
            if directory.joinpath('cgroup').read_text().strip() != '0::' + row['cgroup']:
                raise RuntimeError('RT process cgroup changed; restore before retry')
            try:
                (CGROUP / 'cgroup.procs').write_text(str(row['pid']))
            except OSError as error:
                raise RuntimeError('RT migration failed for pid=' + str(row['pid'])
                                   + ' cgroup=' + row['cgroup'] + ': ' + str(error)) from error
        except (FileNotFoundError, ProcessLookupError):
            continue
    if realtime_groups():
        raise RuntimeError('non-root RT tasks remain; restore before retry')
    # Use the kernel's actual controller interface as the smallest disproof.
    try:
        (CGROUP / 'cgroup.subtree_control').write_text('+cpu')
    except OSError as error:
        raise RuntimeError('CPU controller enable failed after RT migration: ' + str(error)) from error
    if 'cpu' not in (CGROUP / 'cgroup.subtree_control').read_text().split():
        raise RuntimeError('CPU controller did not enable')
    print('ANOLIS_CPU_PREREQUISITE_PASS rt_processes=' + str(len(records)), flush=True)


def restore(run_id):
    require_idle()
    if JOURNAL.is_symlink() or not JOURNAL.is_file():
        raise RuntimeError('missing or substituted RT fixture journal')
    journal = json.loads(JOURNAL.read_text())
    if (journal.get('schema') != 1 or journal.get('host') != '192.168.47.141'
            or journal.get('run_id') != run_id):
        raise RuntimeError('foreign RT fixture journal')
    if journal['boot_id'] == boot_id():
        print('ANOLIS_CPU_RESTORE_REBOOT_REQUIRED', flush=True)
        return 42
    JOURNAL.unlink()
    print('ANOLIS_CPU_RESTORE_PASS', flush=True)
    return 0


def main():
    addresses = subprocess.run(['ip', '-4', '-o', 'addr', 'show'], capture_output=True,
                               text=True, timeout=10, check=True).stdout
    if '192.168.47.141/' not in addresses or os.geteuid() != 0:
        raise RuntimeError('fixture is restricted to authorized Anolis host 141 as root')
    if (len(sys.argv) != 3 or sys.argv[1] not in ('prepare', 'restore')
            or not re.fullmatch(r'[A-Za-z0-9-]+', sys.argv[2])):
        raise RuntimeError('usage: lab-anolis-cgroup.py prepare|restore <owned-run-id>')
    with open('/var/lock/kubeauto-anolis-rt-fixture.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if sys.argv[1] == 'prepare':
            prepare(sys.argv[2])
            return 0
        return restore(sys.argv[2])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('ANOLIS_CPU_FIXTURE_FAIL reason=' + str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)
