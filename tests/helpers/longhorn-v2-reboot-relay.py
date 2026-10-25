#!/usr/bin/env python3
"""Pause only the original Registry observer across its owned worker reboot."""
import json
import os
from pathlib import Path
import signal
import time

KEY = 'reboot_relay'
RECORD = 'longhorn-install-business.json'
LIMIT = 1200


def bound(lab, journal, proof):
    lab.require_lease()
    lab.load_helper('longhorn-v2-runtime-faults.py').validate_intent(lab, journal, proof)
    if proof.get('case') != 'worker-reboot' or lab.relay_rows(lab.owner()):
        raise ValueError('worker reboot pause requires its original direct Registry fixture')


def process(lab, row):
    pid = row['pid']
    identity = lab.process_identity(pid)
    expected = ([value.encode() for value in row['argv']], row['start_tick'])
    if (not identity or identity != expected or
            row['control_boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip() or
            (Path('/proc')/str(pid)).stat().st_uid != os.geteuid()):
        raise ValueError('worker reboot Registry observer identity changed')
    root = Path('/proc')/str(pid)
    state = (root/'stat').read_text().rsplit(')', 1)[1].split()[0]
    children = (root/'task'/str(pid)/'children').read_text().split()
    return state, children


def persist(lab, journal, proof):
    journal['v2_runtime_fault'] = proof
    lab.write_private(lab.DIRECTORY/RECORD, journal)


def pause(lab, journal, proof, pid):
    bound(lab, journal, proof)
    if proof.get('case') != 'worker-reboot' or KEY in proof or type(pid) is not int or pid <= 0:
        raise ValueError('worker reboot requires its original live Registry observer')
    identity = lab.process_identity(pid)
    expected = [str(Path(__file__).with_name('longhorn-v2-install.py')).encode(), b'relay-maintain']
    if not identity or len(identity[0]) != 3 or identity[0][-2:] != expected:
        raise ValueError('worker reboot requires the exact V2 Registry observer argv')
    row = {'pid': pid, 'argv': [value.decode() for value in identity[0]], 'start_tick': identity[1],
           'control_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
           'started': time.monotonic(), 'phase': 'pause-intent'}
    state, _ = process(lab, row)
    if state in ('T', 't'):
        raise ValueError('worker reboot refuses an already stopped Registry observer')
    # Intent precedes the first signal. Stop only at a quiescent point: an SSH
    # child must finish before reboot, otherwise its delayed rc=255 would still
    # be consumed by the old observer after SIGCONT.
    proof[KEY] = row
    persist(lab, journal, proof)
    while True:
        process(lab, row)
        os.kill(pid, signal.SIGSTOP)
        until = time.monotonic() + 5
        while True:
            state, children = process(lab, row)
            if state in ('T', 't'):
                break
            if time.monotonic() >= until:
                raise TimeoutError('Registry observer did not stop')
            time.sleep(.02)
        if not children:
            row['phase'] = 'paused'
            persist(lab, journal, proof)
            print('LONGHORN_V2_REBOOT_RELAY_PAUSED pid='+str(pid), flush=True)
            return
        process(lab, row)
        os.kill(pid, signal.SIGCONT)
        if time.monotonic()-row['started'] >= 60:
            raise TimeoutError('Registry observer has no bounded quiescent point')
        time.sleep(.1)


def resume(lab, journal, proof):
    row = proof.get(KEY)
    if row is None:
        return
    bound(lab, journal, proof)
    process(lab, row)
    if row['phase'] == 'resumed':
        return
    if row['phase'] not in ('pause-intent', 'paused', 'resume-intent'):
        raise ValueError('worker reboot Registry observer phase changed')
    row['phase'] = 'resume-intent'
    persist(lab, journal, proof)
    process(lab, row)
    os.kill(row['pid'], signal.SIGCONT)
    until = time.monotonic()+5
    while process(lab, row)[0] in ('T', 't'):
        if time.monotonic() >= until:
            raise TimeoutError('Registry observer did not resume')
        time.sleep(.02)
    # Always resume before any failing HTTP observation or timeout check. The
    # original transport implementation keeps all socket/rule ownership checks.
    lab.relay_ensure()
    process(lab, row)
    elapsed = time.monotonic()-row['started']
    if not 0 <= elapsed <= LIMIT:
        raise ValueError('worker reboot Registry pause exceeded its native recovery budget')
    row.update(phase='resumed', elapsed=elapsed)
    persist(lab, journal, proof)
    print('LONGHORN_V2_REBOOT_RELAY_RESUMED pid='+str(row['pid'])+' elapsed='+str(int(elapsed)), flush=True)


def resume_current(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('symlink worker reboot journal')
    if path.exists():
        journal = json.loads(path.read_text())
        proof = journal.get('v2_runtime_fault', {})
        if proof.get('case') == 'worker-reboot' and KEY in proof:
            resume(lab, journal, proof)
