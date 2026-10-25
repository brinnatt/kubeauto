#!/usr/bin/env python3
"""V2 full-copy CSI clone qualification, separate from linked-clone acceptance."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

BASE = Path('/usr/local/kubeauto')
NAMESPACE = 'longhorn-system'


def full_copy(lab, journal, payload, digest):
    target = journal['claims']['clone']
    if target['volume'] == journal['volume'] or target['pv'] == journal['pv']:
        raise ValueError('V2 full-copy clone reused the source identity')

    def ready():
        source = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
        clone = lab.get('volumes.longhorn.io', target['volume'], NAMESPACE)
        for obj, uid in ((source, journal['volume_uid']), (clone, target['volume_uid'])):
            if (not obj or obj['metadata']['uid'] != uid or obj['spec'].get('dataEngine') != 'v2' or
                    lab.capacity_bytes(obj['spec']['size']) != 2*1024**3):
                raise ValueError('V2 full-copy source or target identity/engine/capacity changed')
        status = clone.get('status', {}).get('cloneStatus', {})
        if clone['spec'].get('cloneMode') != 'full-copy' or status.get('sourceVolume') != journal['volume']:
            raise ValueError('V2 full-copy clone mode or source differs')
        if status.get('state') == 'failed':
            raise ValueError('V2 full-copy clone failed')
        if status.get('state') != 'completed' or not status.get('snapshot'):
            return None
        topologies = {}
        for key, row in (('source', journal), ('clone', target)):
            topology = lab.replica_topology(row, 3)
            if not topology or not lab.healthy_replicas(row['volume']):
                return None
            topologies[key] = topology
        if ({row['uid'] for row in topologies['source'].values()} &
                {row['uid'] for row in topologies['clone'].values()}):
            raise ValueError('V2 full-copy clone shares a source replica identity')
        replicas = [obj for obj in lab.get('replicas.longhorn.io', namespace=NAMESPACE)['items']
                    if obj['spec'].get('volumeName') == target['volume']]
        if len(replicas) != 3 or any(obj['spec'].get('linkedCloneSrcReplicaName') for obj in replicas):
            raise ValueError('V2 full-copy clone retains linked-replica dependencies')
        return topologies
    topologies = lab.wait('v2-full-copy-completed-three-replicas', 900, ready)
    # Keep the main source payload unchanged for the existing cross-node
    # readback. A separate fsynced source file proves the other write direction.
    code = ("import hashlib,os;data=os.urandom(1024**2);"
            "f=open('/data/clone-source-only','xb',buffering=0);assert f.write(data)==len(data);"
            "os.fsync(f.fileno());f.close();d=os.open('/data',os.O_RDONLY);os.fsync(d);os.close(d);"
            "assert open('/data/clone-source-only','rb').read()==data;print(hashlib.sha256(data).hexdigest())")
    marker_digest = lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-c', code).stdout.strip()
    if not re.fullmatch('[a-f0-9]{64}', marker_digest):
        raise ValueError('V2 source isolation file was not confirmed')
    lab.kubectl('-n', journal['namespace'], 'exec', 'clone-reader', '--', 'python3', '-c',
                "import os;assert not os.path.exists('/data/clone-source-only')")
    expected = hashlib.sha256((payload+'-clone').encode()*262144).hexdigest()
    actual = lab.kubectl('-n', journal['namespace'], 'exec', 'clone-reader', '--', 'python3', '-c',
                         lab.payload_program(payload+'-clone', read_only=True)).stdout.strip()
    source = lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-c',
                         lab.payload_program(payload, read_only=True)).stdout.strip()
    if actual != expected or source != digest or source == actual:
        raise ValueError('V2 clone/source writes crossed their independent data boundaries')
    result = {'source_volume': journal['volume'], 'source_uid': journal['volume_uid'],
              'clone_volume': target['volume'], 'clone_uid': target['volume_uid'],
              'source_sha256': source, 'clone_sha256': actual, 'source_only_bytes': 1024**2,
              'source_only_sha256': marker_digest, 'source_only_absent_from_clone': True,
              'clone_mode': 'full-copy', 'clone_state': 'completed', 'replicas': topologies,
              'csi_size_refusal_verified': True}
    print('LONGHORN_V2_FULL_COPY_CLONE_OBSERVED '+json.dumps(result, sort_keys=True), flush=True)
    return result


def business(lab, relay_pid):
    original = lab.verify_clone
    program = lab.payload_program
    remount = {'checks': 0}
    def verify(journal, create, payload, digest):
        original(journal, create, payload, digest)
        result = full_copy(lab, journal, payload, digest)
        def with_source_file(value, **options):
            code = program(value, **options)
            if value == payload and options.get('read_only'):
                remount['checks'] += 1
                code += (";data=open('/data/clone-source-only','rb').read();assert len(data)==1048576;"
                         "assert hashlib.sha256(data).hexdigest()=="+repr(result['source_only_sha256']))
            return code
        lab.payload_program = with_source_file
    lab.verify_clone = verify
    try:
        lab.run_requests(relay_pid, [])
        lab.business('clone', relay_pid=relay_pid)
        if not remount['checks']:
            raise ValueError('V2 full-copy source-only file was not checked across remount')
        print('LONGHORN_V2_CLONE_SOURCE_FILE_REMOUNT_VERIFIED', flush=True)
    finally:
        lab.verify_clone = original
        lab.payload_program = program


def verify_log(log):
    rows = re.findall(r'^LONGHORN_V2_FULL_COPY_CLONE_OBSERVED (.+)$', log, re.M)
    if len(rows) != 1:
        raise ValueError('V2 full-copy qualifier lacks one complete observation')
    result = json.loads(rows[0])
    if (result['source_volume'] == result['clone_volume'] or not result['source_uid'] or not result['clone_uid'] or
            result['source_uid'] == result['clone_uid'] or result['source_only_bytes'] != 1024**2 or
            result['clone_mode'] != 'full-copy' or result['clone_state'] != 'completed' or
            result['source_only_absent_from_clone'] is not True or result['csi_size_refusal_verified'] is not True):
        raise ValueError('V2 full-copy result lacks independent identities and clone/data boundaries')
    if (not all(re.fullmatch('[a-f0-9]{64}', result[key]) for key in
                ('source_sha256', 'clone_sha256', 'source_only_sha256')) or
            result['source_sha256'] == result['clone_sha256']):
        raise ValueError('V2 full-copy result lacks confirmed independent writes')
    topologies = result['replicas']
    if set(topologies) != {'source', 'clone'} or any(
            len(rows) != 3 or len({row['node'] for row in rows.values()}) != 3 or
            len({row['uid'] for row in rows.values()}) != 3 or
            any(not row['uid'] for row in rows.values()) for rows in topologies.values()):
        raise ValueError('V2 full-copy result lacks independent three-replica topologies')
    if {row['uid'] for row in topologies['source'].values()} & {row['uid'] for row in topologies['clone'].values()}:
        raise ValueError('V2 full-copy result shares source replicas')
    remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    if (len(remounts) != 1 or int(remounts[0][0]) <= 0 or remounts[0][1] != result['source_sha256'] or
            set(remounts[0][2].split(',')) != {row['node'] for row in topologies['source'].values()}):
        raise ValueError('V2 full-copy result lacks original source cross-node data')
    for marker in ('LONGHORN_CLONE_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                   'LONGHORN_V2_CLONE_SOURCE_FILE_REMOUNT_VERIFIED'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('V2 full-copy result lacks business/cleanup marker')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'verify', 'lifecycle-business', 'lifecycle-verify'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    lifecycle = None
    if args.mode.startswith('lifecycle-'):
        spec = importlib.util.spec_from_file_location('full_copy_lifecycle', Path(__file__).with_name('longhorn-v2-clone-lifecycle.py'))
        lifecycle = importlib.util.module_from_spec(spec); spec.loader.exec_module(lifecycle)
    if args.mode in ('verify', 'lifecycle-verify'):
        if not args.log: parser.error('verify requires --log')
        (lifecycle.verify_log if lifecycle else verify_log)(args.log.read_text())
        return
    if args.relay_pid <= 0: parser.error('business requires a positive --relay-pid')
    spec = importlib.util.spec_from_file_location('v2_clone_install', BASE/'tests/helpers/longhorn-v2-install.py')
    install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
    _, lab = install.fixture()
    lab.require_lease()
    (lifecycle.business if lifecycle else business)(lab, args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-clone-failure-', suffix='.txt',
                dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_CLONE_FAIL private_diagnostic='+diagnostic.name, file=sys.stderr)
        sys.exit(1)
