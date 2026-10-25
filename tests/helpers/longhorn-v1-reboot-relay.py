#!/usr/bin/env python3
"""Keep the exact owned V1 Registry observer across one intentional worker boot."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time

RECORD = 'longhorn-v1-reboot-relay.json'


def load(name):
    spec = importlib.util.spec_from_file_location('v1_reboot_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def save(lab, journal, proof):
    lab.write_private(lab.DIRECTORY/RECORD, journal)


def bound(lab, proof):
    lab.require_lease()
    state = lab.owner()
    if (any(proof.get(k) != state[k] for k in ('run_id', 'cluster_uid')) or
            proof['address'] not in lab.WORKERS):
        raise ValueError('V1 worker reboot differs from the leased cluster')
    rows = lab.relay_rows(state)
    matches = [row for row in rows if row['node'] == proof['address']]
    if (len(matches) > 1 or (proof.get('had_relay', True) and not matches) or
            (matches and matches[0]['socket'] != proof['socket'])):
        raise ValueError('V1 worker reboot lacks its original relay identity')
    return state, matches[0] if matches else None


def repair(lab, proof):
    """Recover only the old journaled socket/rule after the same machine booted."""
    common = load('longhorn-v1-stability.py')
    def ready():
        current = common.host(lab, proof['address'], unavailable=True)
        if current is None:
            return None
        if current['machine_id'] != proof['original_host']['machine_id']:
            raise ValueError('V1 Registry reboot changed physical machine identity')
        return current if current['boot_id'] != proof['original_host']['boot_id'] else None
    current = lab.wait('v1-registry-original-worker-new-boot', 1200, ready)
    state, row = bound(lab, proof)
    if not proof.get('had_relay', True):
        # Preserve the original direct Registry probe and all of its fallback
        # socket/listener/rule guards; a direct node need not acquire a relay.
        lab.relay_ensure()
        proof['restored_host'] = current
        return current
    status = lab.remote(row['node'], lab.relay_rule('-C'), check=False).returncode
    if status not in (0, 1):
        raise ValueError('V1 reboot cannot inspect its original Registry rule')
    listener = json.loads(lab.remote(row['node'], ['python3', '-c', lab.listener_script()],
        stdin=json.dumps({'port': lab.PORT, 'action': 'probe'})).stdout)
    if listener is None:
        # A surviving local master can briefly report healthy after its remote
        # machine rebooted. Close only this recorded control socket before reuse.
        lab.execute(lab.SSH+['-S', row['socket'], '-O', 'exit', 'root@'+row['node']], check=False)
        path = Path(row['socket'])
        if path.is_symlink():
            raise ValueError('V1 reboot refuses a symlink Registry socket')
        if path.exists():
            path.unlink()
        lab.relay_connect(row); lab.bind_listener(row, state)
    elif listener != row.get('listener') or listener['boot_id'] != current['boot_id']:
        raise ValueError('V1 reboot refuses a replacement Registry listener')
    if status == 1:
        lab.remote(row['node'], lab.relay_rule('-A'))
    lab.relay_ensure()
    proof['restored_host'] = current
    return current


def pause(lab, journal, proof, pid, supervisor):
    identity = lab.process_identity(pid)
    expected = [str(Path(__file__).with_name('longhorn-lab.py')).encode(), b'relay-maintain']
    if type(pid) is not int or pid <= 0 or not identity or len(identity[0]) != 3 or identity[0][-2:] != expected:
        raise ValueError('V1 reboot requires its exact live Registry observer')
    row = {'pid': pid, 'argv': [value.decode() for value in identity[0]], 'start_tick': identity[1],
        'control_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'started': time.monotonic(), 'phase': 'pause-intent'}
    if supervisor.process(lab, row)[0] in ('T', 't'):
        raise ValueError('V1 reboot refuses an already stopped Registry observer')
    proof[supervisor.KEY] = row; save(lab, journal, proof)
    while True:
        supervisor.process(lab, row); os.kill(pid, signal.SIGSTOP)
        until = time.monotonic()+5
        while True:
            state, children = supervisor.process(lab, row)
            if state in ('T', 't'):
                break
            if time.monotonic() >= until:
                raise TimeoutError('V1 Registry observer did not stop')
            time.sleep(.02)
        if not children:
            row['phase'] = 'paused'; save(lab, journal, proof)
            print('LONGHORN_V1_REBOOT_RELAY_PAUSED pid='+str(pid), flush=True)
            return
        supervisor.process(lab, row); os.kill(pid, signal.SIGCONT)
        if time.monotonic()-row['started'] >= 60:
            raise TimeoutError('V1 Registry observer has no bounded quiescent point')
        time.sleep(.1)


def release(lab, journal, proof, supervisor):
    row = proof.get(supervisor.KEY)
    if row is None:
        return
    # Resume before any failing persistence or timeout check, including an
    # interruption during pause. Bind the original PID again before signalling.
    supervisor.process(lab, row); os.kill(row['pid'], signal.SIGCONT)
    until = time.monotonic()+5
    while supervisor.process(lab, row)[0] in ('T', 't'):
        if time.monotonic() >= until:
            raise TimeoutError('V1 Registry observer did not resume')
        time.sleep(.02)
    row['phase'] = 'resumed'; save(lab, journal, proof)
    if not 0 <= time.monotonic()-row['started'] <= 1200:
        raise ValueError('V1 reboot Registry pause exceeded its recovery budget')
    print('LONGHORN_V1_REBOOT_RELAY_RESUMED pid='+str(row['pid']), flush=True)


def recover_current(lab, *, retire=False):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('V1 reboot refuses a symlink recovery journal')
    if not path.exists():
        return
    journal = json.loads(path.read_text()); proof = journal['reboot']
    bound(lab, proof)
    supervisor = load('longhorn-v2-reboot-relay.py')
    row = proof.get(supervisor.KEY)
    def resume():
        if row and lab.process_identity(row['pid']) is not None:
            release(lab, journal, proof, supervisor)
    with lab.scoped_cleanup(resume):
        if proof.get('reboot_intent'):
            repair(lab, proof)
    if retire and row and lab.process_identity(row['pid']) is not None:
        supervisor.process(lab, row); os.kill(row['pid'], signal.SIGTERM)
        lab.wait('v1-reboot-retired-original-registry-observer', 30,
            lambda: lab.process_identity(row['pid']) is None)
    path.unlink()


def reboot(lab, pid, address, original):
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink():
        raise ValueError('recover the prior V1 worker Registry reboot first')
    state = lab.owner()
    proof = {k: state[k] for k in ('run_id', 'cluster_uid')}
    proof.update(address=address, original_host=original,
        had_relay=any(row['node'] == address for row in lab.relay_rows(state)),
        socket=str(lab.DIRECTORY/('longhorn-registry-'+address+'.sock')))
    _, row = bound(lab, proof)
    if row and row.get('listener', {}).get('boot_id') != original['boot_id']:
        raise ValueError('V1 reboot original relay boot identity differs')
    if load('longhorn-v1-stability.py').host(lab, address) != original:
        raise ValueError('V1 worker changed before Registry pause')
    supervisor = load('longhorn-v2-reboot-relay.py')
    journal = {'reboot': proof}
    def resume():
        release(lab, journal, proof, supervisor)
    with lab.scoped_cleanup(resume):
        pause(lab, journal, proof, pid, supervisor)
        proof['reboot_intent'] = True
        try:
            save(lab, journal, proof)
        except BaseException:
            proof['reboot_intent'] = False
            raise
        load('longhorn-v2-runtime-faults.py').host_call(lab, address,
            {'action': 'reboot', 'expected': original}, check=False)
        current = repair(lab, proof)
    path.unlink()
    print('LONGHORN_V1_REBOOT_REGISTRY_RECOVERED '+json.dumps(proof, sort_keys=True), flush=True)
    return current


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda number, frame: sys.exit(128+number))
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('recover-current',))
    parser.parse_args()
    lab = load('longhorn-lab.py'); lab.require_lease()
    recover_current(lab, retire=True)
