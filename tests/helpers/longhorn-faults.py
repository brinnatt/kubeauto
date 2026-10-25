"""Bounded data-plane faults for volumes created by the Longhorn runner."""
import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid


BLOCK = 65536


def ledger_block(token, sequence):
    return hashlib.sha256((token + ':' + str(sequence)).encode()).digest() * (BLOCK // 32)


def verify_ledger(path, token, records):
    """Verify every acknowledged slot; an unacknowledged tail is not committed."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size < len(records) * BLOCK:
            raise ValueError('ledger is not a complete regular data file')
        digest = hashlib.sha256()
        for sequence, record in enumerate(records):
            expected = ledger_block(token, sequence)
            if (record != {'type': 'ack', 'sequence': sequence, 'sha256': hashlib.sha256(expected).hexdigest()} or
                    os.pread(descriptor, BLOCK, sequence * BLOCK) != expected):
                raise ValueError('acknowledged ledger record changed')
            digest.update(expected)
        return {'confirmed': len(records), 'verified_bytes': len(records) * BLOCK, 'sha256': digest.hexdigest()}
    finally:
        os.close(descriptor)


def write_ledger(path, token, duration=180, interval=0.25, stop=None):
    """Acknowledge only after fsync; retry an unacknowledged slot with identical bytes."""
    if not token or not 0 < duration <= 1800 or not 0 <= interval <= 5:
        raise ValueError('invalid ledger workload')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    records = []; errors = {}; worst = 0.0
    try:
        parent = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if stop is not None and stop():
                break
            started = time.monotonic()
            sequence = len(records); data = ledger_block(token, sequence)
            try:
                if os.pwrite(descriptor, data, sequence * BLOCK) != len(data):
                    raise ValueError('short ledger write')
                os.fsync(descriptor)
            except OSError as error:
                if error.errno not in (errno.EIO, errno.ETIMEDOUT, errno.ESTALE, errno.EROFS):
                    raise
                key = errno.errorcode[error.errno]
                errors[key] = errors.get(key, 0) + 1
                print(json.dumps({'type': 'retry', 'sequence': sequence, 'errno': key}), flush=True)
            else:
                record = {'type': 'ack', 'sequence': sequence, 'sha256': hashlib.sha256(data).hexdigest()}
                records.append(record)
                print(json.dumps(record, sort_keys=True), flush=True)
            worst = max(worst, time.monotonic() - started)
            time.sleep(interval)
    finally:
        os.close(descriptor)
    result = verify_ledger(path, token, records)
    result.update(type='complete', errors=errors, retries=sum(errors.values()), max_latency_s=worst)
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def ledger_stop_requested():
    if not select.select([sys.stdin], [], [], 0)[0]:
        return False
    if sys.stdin.readline() != 'stop\n':
        raise RuntimeError('ledger stop channel lost or invalid')
    return True


def ledger_program(token, duration=1500):
    return ('import errno,hashlib,json,os,select,stat,sys,time\nBLOCK=65536\n' +
            '\n'.join(inspect.getsource(function) for function in
                      (ledger_block, verify_ledger, write_ledger, ledger_stop_requested)) +
            '\nwrite_ledger("/data/fault-ledger",%r,duration=%r,stop=ledger_stop_requested)\n' % (token, duration))


def replica_process(lab, journal, replica):
    """Resolve the original Volume -> Replica -> InstanceManager -> Pod chain."""
    volume = lab.get('volumes.longhorn.io', journal['volume'], 'longhorn-system')
    current = lab.get('replicas.longhorn.io', replica['metadata']['name'], 'longhorn-system')
    if (not volume or volume['metadata']['uid'] != journal['volume_uid'] or
            not current or current['metadata']['uid'] != replica['metadata']['uid'] or
            current['spec'].get('volumeName') != journal['volume'] or current['spec'].get('failedAt') or
            not any(ref.get('kind') == 'Volume' and ref.get('name') == journal['volume'] and
                    ref.get('uid') == journal['volume_uid'] for ref in current['metadata'].get('ownerReferences', [])) or
            current.get('status', {}).get('currentState') != 'running'):
        raise ValueError('replica fault source identity or state changed')
    node = current['spec']['nodeID']
    allowed = {'longhorn-node-' + ip.rsplit('.', 1)[1] for ip in lab.WORKERS}
    if node not in allowed or current['spec'].get('diskPath') != lab.DATA:
        raise ValueError('replica fault is outside the owned storage boundary')
    directory = current['spec'].get('dataDirectoryName', '')
    if not directory or Path(directory).name != directory or directory in ('.', '..'):
        raise ValueError('replica fault data directory is not a single path component')
    manager_name = current['status'].get('instanceManagerName')
    manager = lab.get('instancemanagers.longhorn.io', manager_name, 'longhorn-system') if manager_name else None
    pod = lab.get('pod', manager_name, 'longhorn-system') if manager_name else None
    if (not manager or manager['spec'].get('nodeID') != node or manager['spec'].get('dataEngine') != 'v1' or
            manager.get('status', {}).get('currentState') != 'running' or not pod or
            pod['metadata'].get('deletionTimestamp') or pod['spec'].get('nodeName') != node or
            not any(ref.get('kind') == 'InstanceManager' and ref.get('uid') == manager['metadata']['uid'] and
                    ref.get('controller') for ref in pod['metadata'].get('ownerReferences', [])) or
            not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', []))):
        raise ValueError('replica fault instance manager identity or readiness changed')
    containers = pod['spec']['containers']
    tag = json.loads((lab.BASE/'roles/longhorn/files/images.json').read_text())['1.12.1']['longhorn-instance-manager']
    if (len(containers) != 1 or containers[0]['name'] != 'instance-manager' or
            not containers[0]['image'].endswith('/longhorn-instance-manager:'+tag) or
            not any(v.get('secret', {}).get('secretName') == 'longhorn-grpc-tls' for v in pod['spec'].get('volumes', []))):
        raise ValueError('replica fault requires the pinned mTLS instance manager')
    result = lab.kubectl('-n', 'longhorn-system', 'exec', manager_name, '-c', 'instance-manager', '--',
                         'longhorn-instance-manager', '--tls-dir', '/tls-files/', 'process', 'get',
                         '--name', current['metadata']['name'], timeout=30)
    process = json.loads(result.stdout)
    specification = process.get('spec', {}); state = process.get('status', {})
    identity = state.get('uuid', '')
    try:
        uuid.UUID(identity)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('replica process has no valid native UUID') from error
    # Manager 1.12.1 GetReplicaMountedDataPath maps the host path under /host.
    if (specification.get('name') != current['metadata']['name'] or state.get('state') != 'running' or
            not specification.get('binary', '').endswith('/longhorn') or
            'replica' not in specification.get('args', []) or
            '/host' + lab.DATA + '/replicas/' + directory not in specification.get('args', [])):
        raise ValueError('native replica process differs from the original data identity')
    return {'manager': manager_name, 'manager_uid': manager['metadata']['uid'], 'pod_uid': pod['metadata']['uid'],
            'name': current['metadata']['name'], 'replica_uid': current['metadata']['uid'], 'uuid': identity, 'node': node}


def stop_replica_process(lab, journal, replica):
    original = replica_process(lab, journal, replica)
    # Resolve immediately again. The RPC UUID is an additional server-side
    # precondition even if a Pod or same-name native process races this read.
    if replica_process(lab, journal, replica) != original:
        raise ValueError('replica process changed before the fault')
    result = lab.kubectl('-n', 'longhorn-system', 'exec', original['manager'], '-c', 'instance-manager', '--',
                         'longhorn-instance-manager', '--tls-dir', '/tls-files/', 'process', 'delete',
                         '--name', original['name'], '--uuid', original['uuid'], timeout=30)
    deleted = json.loads(result.stdout)
    if (deleted.get('spec', {}).get('name') != original['name'] or
            deleted.get('status', {}).get('uuid') != original['uuid'] or deleted.get('deleted') is not True):
        raise ValueError('native process deletion did not confirm the original UUID')
    print('LONGHORN_REPLICA_PROCESS_STOPPED ' + json.dumps(original, sort_keys=True), flush=True)
    return original


def verify_client_ledger(lab, journal, client, proof):
    code = ('import hashlib,json,os,stat,sys\nBLOCK=65536\n' + inspect.getsource(ledger_block) + '\n' +
            inspect.getsource(verify_ledger) + '\np=json.load(sys.stdin)\n'
            'print(json.dumps(verify_ledger("/data/fault-ledger",p["token"],p["records"]),sort_keys=True))')
    actual = json.loads(lab.kubectl('-n', journal['namespace'], 'exec', '-i', client, '--', 'python3', '-c', code,
                                  stdin=json.dumps({'token': proof['token'], 'records': proof['records']}), timeout=120).stdout)
    if actual != proof['summary']:
        raise ValueError('independent verification differs from acknowledged ledger')


def verify_replica_failure(lab, journal):
    before = lab.wait('replica-fault-original-topology', 600, lambda: lab.replica_topology(journal, 3))
    replicas = [r for r in lab.get('replicas.longhorn.io', namespace='longhorn-system')['items']
                if r['metadata']['name'] in before]
    source = next(r for r in replicas if r['spec']['nodeID'] != 'longhorn-node-' + lab.WORKERS[0].rsplit('.', 1)[1])
    token = lab.owner()['run_id'] + ':replica-failure'
    observations = Path(tempfile.mkdtemp(prefix='kubeauto-longhorn-replica-fault-', dir='/var/tmp'))
    log = observations / 'ledger.log'; success = False
    def rows():
        return [json.loads(line) for line in log.read_text().splitlines(keepends=True)
                if line.startswith('{') and line.endswith('\n')]
    def acknowledgements():
        return [row for row in rows() if row.get('type') == 'ack']
    with log.open('w') as stream:
        process = subprocess.Popen([str(lab.BASE / 'kube-bin/kubectl'),
            '--kubeconfig=' + str(lab.DIRECTORY / 'kubectl.kubeconfig'), '-n', journal['namespace'],
            'exec', '-i', 'writer', '--', 'python3', '-u', '-c', ledger_program(token)],
            stdin=subprocess.PIPE, stdout=stream, stderr=stream, text=True)
        try:
            def started():
                if process.poll() is not None:
                    raise ValueError('ledger client exited before the replica fault')
                return len(acknowledgements()) >= 4
            lab.wait('replica-fault-ledger-started', 60, started)
            count_before = len(acknowledgements()); started_at = time.monotonic()
            stopped = stop_replica_process(lab, journal, source)
            def failed():
                replica = lab.get('replicas.longhorn.io', stopped['name'], 'longhorn-system')
                return not replica or replica['metadata']['uid'] != stopped['replica_uid'] or bool(replica['spec'].get('failedAt'))
            lab.wait('replica-process-failure-observed', 120, failed)
            after = lab.wait('replica-fault-rebuilt', 1200, lambda: lab.replica_topology(journal, 3))
            for name, identity in before.items():
                if name != stopped['name'] and after.get(name) != identity:
                    raise ValueError('single-replica fault replaced a healthy survivor')
            rebuilt_s = time.monotonic() - started_at
            count_rebuilt = len(acknowledgements())
            def resumed():
                if len(acknowledgements()) >= count_rebuilt + 4:
                    return True
                if process.poll() is not None:
                    raise ValueError('ledger client stopped before post-rebuild writes')
                return False
            lab.wait('replica-fault-post-rebuild-writes', 60, resumed)
            process.stdin.write('stop\n'); process.stdin.flush()
            lab.wait('replica-fault-ledger-complete', 60, lambda: process.poll() is not None)
            if process.returncode != 0:
                raise ValueError('replica fault ledger client failed')
            records = acknowledgements(); completed = [row for row in rows() if row.get('type') == 'complete']
            if len(completed) != 1 or len(records) < count_before + 4 or completed[0]['confirmed'] != len(records):
                raise ValueError('replica fault has no complete acknowledged-write ledger')
            proof = {'token': token, 'records': records,
                     'summary': {key: completed[0][key] for key in ('confirmed', 'verified_bytes', 'sha256')}}
            verify_client_ledger(lab, journal, 'writer', proof)
            print('LONGHORN_REPLICA_FAULT_LEDGER_VERIFIED ' + json.dumps(dict(completed[0],
                confirmed_before_fault=count_before, confirmed_at_rebuild=count_rebuilt, rebuild_s=round(rebuilt_s, 3),
                acknowledgements_sha256=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()), sort_keys=True), flush=True)
            print('LONGHORN_REPLICA_FAULT_ACKNOWLEDGEMENTS ' + json.dumps(records, sort_keys=True), flush=True)
            print('LONGHORN_REPLICA_FAULT_PLACEMENT ' + json.dumps({'before': before, 'after': after}, sort_keys=True), flush=True)
            success = True
            return proof
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=10)
            if success:
                shutil.rmtree(observations)
            else:
                print('LONGHORN_PRIVATE_DIAGNOSTIC directory=' + str(observations), flush=True)
