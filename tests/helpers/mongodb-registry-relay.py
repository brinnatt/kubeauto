#!/usr/bin/env python3
"""Owned Registry transport for the NAT-separated MongoDB lab.

Reuse the established Ceph qualification relay mechanism with independent
state, sockets, port and rule comment. No production registry or route changes.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time

NODES = ('192.168.122.135', '192.168.122.40', '192.168.122.72',
         '192.168.122.212', '192.168.122.165', '192.168.122.238')
DIRECTORY = Path('/usr/local/kubeauto/clusters/mongodb-gate')
STATE = DIRECTORY / 'mongodb-registry-relay.json'
REGISTRY = '192.168.47.130'
PORT = 23510
COMMENT = 'kubeauto-mongodb-registry'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
       '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2']


def execute(command, check=True):
    if '-f' in command:
        # ssh -f's background master retains descriptors; a pipe would wait
        # forever after its launcher returned. Preserve stderr in a private file.
        with tempfile.TemporaryFile(mode='w+') as errors:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=errors, text=True, timeout=40)
            errors.seek(0)
            result.stdout, result.stderr = '', errors.read()
    else:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
    if check and result.returncode:
        raise ValueError('MongoDB Registry relay command failed rc=' + str(result.returncode) +
                         '; preserve runner diagnostics')
    return result


def remote(node, command, check=True):
    if node not in NODES:
        raise ValueError('MongoDB Registry relay refuses a foreign host')
    try:
        return execute([*SSH, 'root@' + node, command], check=check)
    except ValueError as exc:
        raise ValueError('MongoDB Registry relay remote failed node=' + node + '; ' + str(exc)) from exc


def rule(operation):
    if operation not in ('-C', '-A', '-D'):
        raise ValueError('MongoDB Registry relay refuses an unknown rule operation')
    return shlex.join(['iptables', '-w', '5', '-t', 'nat', operation, 'OUTPUT', '-p', 'tcp',
                       '-d', REGISTRY + '/32', '--dport', '5000', '-m', 'comment', '--comment', COMMENT,
                       '-j', 'REDIRECT', '--to-ports', str(PORT)])


def probe(node):
    command = shlex.join(['curl', '--fail', '--silent', '--show-error', '--noproxy', '*',
                          '--max-time', '5', 'http://' + REGISTRY + ':5000/v2/'])
    return remote(node, command, check=False).returncode == 0


def save(rows):
    with tempfile.NamedTemporaryFile('w', dir=DIRECTORY, delete=False) as stream:
        json.dump(rows, stream)
        stream.flush()
        os.fsync(stream.fileno())
        path = Path(stream.name)
    path.chmod(0o600)
    path.replace(STATE)


def journal():
    if DIRECTORY.is_symlink() or STATE.is_symlink():
        raise ValueError('MongoDB Registry relay refuses a symlink state path')
    rows = json.loads(STATE.read_text())
    if not isinstance(rows, list) or len(rows) > len(NODES):
        raise ValueError('MongoDB Registry relay journal is invalid')
    seen = set()
    for row in rows:
        if (set(row) != {'node', 'socket'} or row['node'] not in NODES or row['node'] in seen or
                row['socket'] != str(DIRECTORY / ('mongodb-registry-' + row['node'] + '.sock'))):
            raise ValueError('MongoDB Registry relay refuses a foreign journal entry')
        seen.add(row['node'])
    return rows


def connect(row):
    log = DIRECTORY / ('mongodb-registry-' + row['node'] + '.log')
    if log.is_symlink():
        raise ValueError('MongoDB Registry relay refuses a symlink diagnostic log')
    descriptor = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    execute([*SSH, '-E', str(log), '-M', '-S', row['socket'], '-o', 'ExitOnForwardFailure=yes', '-f', '-N',
             '-R', f'127.0.0.1:{PORT}:127.0.0.1:5000', 'root@' + row['node']])


def reboot_receipt(node):
    path = DIRECTORY / 'mongodb-fault-node.json'
    if not path.exists():return None
    performance = DIRECTORY / 'mongodb-performance.json'
    backup_path = DIRECTORY / 'mongodb-backup-logical.json'
    stability_path = DIRECTORY / 'mongodb-fault-stability.json'
    owner_path = DIRECTORY / 'mongodb-lab-owner.json'
    if any(p.is_symlink() for p in (path, performance, backup_path, stability_path, owner_path)):
        raise ValueError('MongoDB Registry reboot receipt refuses a symlink')
    fault = json.loads(path.read_text())
    backup = json.loads(backup_path.read_text()) if backup_path.exists() else {}
    stability = backup.get('stability')
    if performance.exists():
        if stability is not None or stability_path.exists():
            raise ValueError('MongoDB Registry reboot receipt has ambiguous workload ownership')
        workload = json.loads(performance.read_text())
    else:
        if (not isinstance(stability, dict) or stability.get('topology') not in ('source', 'sharded') or
                not re.fullmatch(r'[0-9a-f]{16}', str(backup.get('run_id', ''))) or
                not stability_path.is_file() or json.loads(stability_path.read_text()) != {
                    'cluster_uid': backup.get('cluster_uid'), 'run_id': backup['run_id'],
                    'workload': stability.get('workload'), 'topology': stability['topology']}):
            raise ValueError('MongoDB Registry reboot receipt lacks its bound stability journals')
        workload = {'cluster_uid': backup.get('cluster_uid'), 'run_id': stability.get('workload'),
                    'client_uid': stability.get('client_uid')}
    owner = json.loads(owner_path.read_text())
    if (fault.get('address') not in NODES[3:] or
            fault.get('node') != 'mongodb-node-' + fault['address'].rsplit('.', 1)[1] or
            fault.get('issued') is not True or fault.get('cluster_uid') != owner.get('cluster_uid') or
            tuple(owner.get('nodes', [])) != NODES or owner.get('clean_start_verified') is not True or
            fault.get('run_id') != workload.get('run_id') or fault.get('client_uid') != workload.get('client_uid') or
            fault.get('cluster_uid') != workload.get('cluster_uid') or
            not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', str(fault.get('run_id', ''))) or
            not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(fault.get('boot_id', '')))):
        raise ValueError('MongoDB Registry reboot receipt differs from its leased workload')
    if node != fault['address']:return None
    result = execute([str(DIRECTORY.parents[1] / 'kube-bin/kubectl'),
        '--kubeconfig=' + str(DIRECTORY / 'kubectl.kubeconfig'), 'get', 'node', fault['node'], '-o', 'json'])
    current = json.loads(result.stdout)
    if (current['metadata']['uid'] != fault.get('node_uid') or
            fault.get('node_uids', {}).get(fault['node']) != fault['node_uid'] or
            [a['address'] for a in current['status']['addresses'] if a['type'] == 'InternalIP'] != [node]):
        raise ValueError('MongoDB Registry reboot node identity changed')
    return fault


def refresh(pending=None):
    for row in journal():
        if execute([*SSH, '-S', row['socket'], '-O', 'check', 'root@' + row['node']], check=False).returncode == 0:
            if pending is not None:pending.pop(row['node'], None)
            continue
        receipt = reboot_receipt(row['node'])
        missing_rule = False
        if receipt:
            try:boot = remote(row['node'], 'cat /proc/sys/kernel/random/boot_id', check=False)
            except subprocess.TimeoutExpired:boot = None
            if boot is None or boot.returncode == 255 or boot.stdout.strip() == receipt['boot_id']:
                if pending is None:
                    raise ValueError('MongoDB Registry reboot transport has not recovered')
                started = pending.setdefault(row['node'], time.monotonic())
                if time.monotonic() - started > 300:
                    raise ValueError('MongoDB Registry reboot transport deadline exceeded')
                print('MONGODB_REGISTRY_REBOOT_HEARTBEAT node=' + row['node'], flush=True)
                continue
            if boot.returncode or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot.stdout.strip()):
                raise ValueError('MongoDB Registry reboot boot identity is invalid')
            rule_state = remote(row['node'], rule('-C'), check=False)
            if rule_state.returncode not in (0, 1):
                raise ValueError('MongoDB Registry reboot rule inspection failed')
            missing_rule = rule_state.returncode == 1
            remote(row['node'], f'test -z "$(ss -H -lnt \'sport = :{PORT}\')"')
        else:
            remote(row['node'], rule('-C'))
        path = Path(row['socket'])
        if path.exists():
            path.unlink()
        connect(row)
        if missing_rule:remote(row['node'], rule('-A'))
        if not probe(row['node']):
            raise ValueError('MongoDB Registry relay reconnect probe failed')
        if pending is not None:pending.pop(row['node'], None)
        print('MONGODB_REGISTRY_RECONNECTED node=' + row['node'], flush=True)


def setup():
    if STATE.exists() or DIRECTORY.is_symlink():
        raise ValueError('MongoDB Registry relay setup refuses previous state')
    owner = json.loads((DIRECTORY / 'mongodb-lab-owner.json').read_text())
    if owner.get('cluster') != 'mongodb-gate' or tuple(owner.get('nodes', [])) != NODES or owner.get('clean_start_verified') is not True:
        raise ValueError('MongoDB Registry relay requires the clean-start owner journal')
    rows = []
    save(rows)
    for node in NODES:
        if probe(node):
            print('MONGODB_REGISTRY_DIRECT_PASS node=' + node, flush=True)
            continue
        socket = DIRECTORY / ('mongodb-registry-' + node + '.sock')
        if socket.exists() or remote(node, rule('-C'), check=False).returncode != 1:
            raise ValueError('MongoDB Registry relay refuses previous rule/socket or an unknown inspection failure')
        remote(node, f'test -z "$(ss -H -lnt \'sport = :{PORT}\')"')
        row = {'node': node, 'socket': str(socket)}
        rows.append(row)
        save(rows)  # Persist before opening a listener or changing NAT.
        connect(row)
        remote(node, rule('-A'))
        if not probe(node):
            raise ValueError('MongoDB Registry relayed endpoint did not return HTTP success')
        print('MONGODB_REGISTRY_RELAY_PASS node=' + node, flush=True)
    print('MONGODB_REGISTRY_TRANSPORT_READY nodes=6 endpoint=unchanged', flush=True)


def cleanup():
    if not STATE.exists():
        return
    rows = journal()  # Validate every entry before any remote mutation.
    for row in rows:
        node = row['node']
        result = remote(node, rule('-C'), check=False)
        if result.returncode == 0:
            remote(node, rule('-D'))
        elif result.returncode != 1:
            raise ValueError('MongoDB Registry relay cannot inspect its rule')
        if remote(node, rule('-C'), check=False).returncode != 1:
            raise ValueError('MongoDB Registry relay rule remains')
        execute([*SSH, '-S', row['socket'], '-O', 'exit', 'root@' + node], check=False)
        deadline = time.monotonic() + 5
        while Path(row['socket']).exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if Path(row['socket']).exists():
            raise ValueError('MongoDB Registry relay socket remains')
        remote(node, f'test -z "$(ss -H -lnt \'sport = :{PORT}\')"')
    STATE.unlink()
    print('MONGODB_REGISTRY_RELAY_CLEAN_PASS rules-sockets-listeners=absent', flush=True)


def qualification():
    first_error = None
    try:
        setup()
        for row in journal():
            execute([*SSH, '-S', row['socket'], '-O', 'exit', 'root@' + row['node']])
            deadline = time.monotonic() + 5
            while Path(row['socket']).exists() and time.monotonic() < deadline:
                time.sleep(.1)
            if Path(row['socket']).exists():
                raise ValueError('MongoDB Registry relay master did not stop')
        refresh()
        if not all(probe(node) for node in NODES):
            raise ValueError('MongoDB Registry relay recovery did not restore every endpoint')
        print('MONGODB_REGISTRY_RELAY_RECOVERY_PASS', flush=True)
    except BaseException as exc:
        first_error = exc
        raise
    finally:
        try:
            cleanup()
        except BaseException:
            print('MONGODB_REGISTRY_RELAY_CLEAN_FAIL journal=preserved', flush=True)
            if first_error is None:
                raise
    print('MONGODB_REGISTRY_RELAY_QUALIFICATION_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('setup', 'ensure', 'maintain', 'cleanup', 'qualification'))
    args = parser.parse_args()
    stopped = threading.Event()
    if args.mode != 'maintain':
        def interrupted(signum, frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, interrupted)
    if args.mode == 'maintain':
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        pending = {}
        while not stopped.is_set():
            refresh(pending)
            print('MONGODB_REGISTRY_RELAY_HEARTBEAT', flush=True)
            stopped.wait(10)
    elif args.mode == 'qualification':
        # The enclosing fixed runner already owns the shared compute leases.
        with open('/var/lock/kubeauto-mongodb-registry.lock', 'a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if STATE.exists():
                raise ValueError('MongoDB Registry qualification refuses existing state')
            qualification()
    elif args.mode == 'ensure':
        refresh() if STATE.exists() else setup()
        if not all(probe(node) for node in NODES):
            raise ValueError('MongoDB Registry transport preflight failed')
    else:
        {'setup': setup, 'cleanup': cleanup}[args.mode]()


if __name__ == '__main__':
    main()
