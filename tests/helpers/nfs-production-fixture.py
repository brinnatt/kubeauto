#!/usr/bin/env python3
"""Temporary export on the exclusively leased Rocky 9.8 worker, never a test disk.

exportfs -u removes only the specified client/export pair; no blanket unexport.
https://man7.org/linux/man-pages/man8/exportfs.8.html
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

STATE = Path('/var/tmp/kubeauto-nfs-production-owner.json')
DATA = Path('/var/tmp/kubeauto-nfs-production-data')
EXPORT = Path('/etc/exports.d/kubeauto-nfs-production.exports')
CLIENTS = ('192.168.122.212', '192.168.122.165', '192.168.122.238')
SERVICES = ('nfs-server.service', 'rpcbind.service', 'rpcbind.socket')
CONTENT = str(DATA)+' '+ ' '.join(h+'(rw,sync,no_subtree_check,no_root_squash)' for h in CLIENTS)+'\n'


def command(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=60)


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def require_host():
    addresses = command('hostname', '-I').stdout.split()
    if '192.168.122.238' not in addresses or os.geteuid() != 0:
        raise RuntimeError('fixture requires the leased root worker 192.168.122.238')


def prepare(run):
    require_host()
    if any(p.exists() or p.is_symlink() for p in (STATE, DATA, EXPORT)):
        raise RuntimeError('foreign or unfinished export fixture')
    states = {unit: command('systemctl', 'is-active', unit, check=False).stdout.strip() for unit in SERVICES}
    if states[SERVICES[0]] != 'inactive' or any(s not in ('active', 'inactive') for s in states.values()):
        raise RuntimeError('NFS server must be inactive and original service states known')
    if command('exportfs', '-s').stdout.strip():
        raise RuntimeError('foreign NFS exports; refusing to reuse this server')
    journal = {'run': run, 'boot_id': boot_id(), 'services': states, 'export': CONTENT}
    descriptor = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as out:
        json.dump(journal, out)
        out.flush()
        os.fsync(out.fileno())
    DATA.mkdir(mode=0o777)
    DATA.chmod(0o777)
    EXPORT.parent.mkdir(exist_ok=True)
    with EXPORT.open('x') as out:
        out.write(CONTENT)
    command('systemctl', 'start', 'rpcbind.service', 'nfs-server.service')
    command('exportfs', '-r')
    if str(DATA) not in command('exportfs', '-s').stdout:
        raise RuntimeError('owned export did not become active')
    print('NFS_SERVER_FIXTURE_PASS')


def cleanup(run):
    require_host()
    if not STATE.exists():
        if DATA.exists() or EXPORT.exists():
            raise RuntimeError('unowned export residue; refusing cleanup')
        return
    if STATE.is_symlink() or DATA.is_symlink() or EXPORT.is_symlink():
        raise RuntimeError('fixture path changed to a symlink')
    journal = json.loads(STATE.read_text())
    if journal['run'] != run or journal['boot_id'] != boot_id() or journal['export'] != CONTENT:
        raise RuntimeError('fixture identity changed')
    if EXPORT.exists() and EXPORT.read_text() != CONTENT:
        raise RuntimeError('owned export declaration changed')
    active = command('exportfs', '-s').stdout
    if any(line.strip() and str(DATA) not in line for line in active.splitlines()):
        raise RuntimeError('foreign export appeared; preserve server and journal')
    if EXPORT.exists():
        if str(DATA) in active:
            for host in CLIENTS:
                command('exportfs', '-u', host+':'+str(DATA))
        EXPORT.unlink()
    command('systemctl', 'stop', 'nfs-server.service')
    for unit in ('rpcbind.socket', 'rpcbind.service'):
        if journal['services'][unit] == 'inactive':
            command('systemctl', 'stop', unit)
    if DATA.exists():
        shutil.rmtree(DATA)
    if str(DATA) in command('exportfs', '-s').stdout:
        raise RuntimeError('owned export remains')
    for unit, original in journal['services'].items():
        if command('systemctl', 'is-active', unit, check=False).stdout.strip() != original:
            raise RuntimeError('original service state not restored: '+unit)
    STATE.unlink()
    print('NFS_SERVER_FIXTURE_CLEAN_PASS')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in ('prepare', 'cleanup'):
        raise SystemExit('usage: nfs-production-fixture.py prepare|cleanup run-id')
    {'prepare':prepare, 'cleanup':cleanup}[sys.argv[1]](sys.argv[2])
