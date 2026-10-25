#!/usr/bin/env python3
"""Narrow V2 remote replica instance-manager Pod loss with acknowledged I/O.

Reuse the established client ledger and remount verifier. V1 process RPCs are
never used: the only injected fault is a UID/RV-guarded Kubernetes Pod DELETE.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'


def owner(obj, kind, uid, *, controller=True):
    return any(r.get('kind') == kind and r.get('uid') == uid and (not controller or r.get('controller') is True)
               for r in obj.get('metadata', {}).get('ownerReferences', []))


def pod_ready(pod):
    return bool(pod and not pod['metadata'].get('deletionTimestamp') and
                pod.get('status', {}).get('phase') == 'Running' and
                any(c.get('type') == 'Ready' and c.get('status') == 'True'
                    for c in pod.get('status', {}).get('conditions', [])))


def boundary(lab, journal):
    ns = lab.get('namespace', NS)
    volumes = lab.get('volumes.longhorn.io', namespace=NS)['items']
    if (not ns or ns['metadata']['uid'] != lab.owner()['namespace_uid'] or
            [(v['metadata']['name'], v['metadata']['uid']) for v in volumes] !=
            [(journal['volume'], journal['volume_uid'])]):
        raise ValueError('V2 shared Pod loss refuses foreign namespace or volume consumers')
    volume = volumes[0]
    nodes = {'longhorn-node-'+ip.rsplit('.', 1)[1] for ip in lab.WORKERS}
    if (volume['spec'].get('dataEngine') != 'v2' or volume['spec'].get('encrypted') or
            volume['spec'].get('accessMode') != 'rwo' or
            volume.get('status', {}).get('state') != 'attached' or
            volume['status'].get('currentNodeID') not in nodes or
            not lab.healthy_replicas(journal['volume']) or not lab.replica_topology(journal, 3)):
        raise ValueError('V2 Pod loss requires the owned healthy three-replica raw-disk volume')
    node = volume['status']['currentNodeID']
    replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
                if r['spec'].get('volumeName') == journal['volume'] and r['spec'].get('nodeID') != node]
    if len(replicas) != 2:
        raise ValueError('V2 Pod loss requires two remote replica choices')
    replica = sorted(replicas, key=lambda r: r['metadata']['name'])[0]
    # GetOwnerReferencesForVolume does not set controller; the IM-to-Pod
    # reference does. Do not invent a different ownership contract here.
    if not owner(replica, 'Volume', journal['volume_uid'], controller=False):
        raise ValueError('remote V2 replica owner changed')
    name = replica.get('status', {}).get('instanceManagerName')
    manager = lab.get('instancemanagers.longhorn.io', name, NS) if name else None
    pod = lab.get('pod', name, NS) if name else None
    if (not manager or manager['spec'].get('dataEngine') != 'v2' or manager['spec'].get('type') != 'aio' or
            manager['spec'].get('nodeID') != replica['spec']['nodeID'] or
            manager.get('status', {}).get('currentState') != 'running' or not pod_ready(pod) or
            pod['spec'].get('nodeName') != manager['spec']['nodeID'] or
            not owner(pod, 'InstanceManager', manager['metadata']['uid'])):
        raise ValueError('remote V2 instance-manager or Pod ownership/readiness differs')
    image = manager['spec'].get('image')
    inventory = json.loads((lab.BASE/'roles/longhorn/files/images.json').read_text())
    tag = inventory['1.12.1']['longhorn-instance-manager']
    containers = pod['spec']['containers']
    if (not image or not image.endswith('/longhorn-instance-manager:'+tag) or
            len(containers) != 1 or containers[0]['name'] != 'instance-manager' or containers[0]['image'] != image or
            '--spdk-enabled' not in containers[0].get('args', []) or
            not any(v.get('secret', {}).get('secretName') == 'longhorn-grpc-tls'
                    for v in pod['spec'].get('volumes', []))):
        raise ValueError('V2 Pod loss requires the pinned SPDK instance-manager with TLS')
    state = manager['status']
    if any(state.get(k) for k in ('instanceEngines', 'instanceEngineFrontends', 'instanceShards',
                                 'instanceShardGroups', 'backingImages')):
        raise ValueError('remote V2 instance-manager contains other native consumers')
    processes = state.get('instanceReplicas') or {}
    if (set(processes) != {replica['metadata']['name']} or
            any(p.get('spec', {}).get('name') != key or p.get('status', {}).get('state') != 'running' or
                p.get('status', {}).get('type') != 'replica' for key, p in processes.items())):
        raise ValueError('remote V2 native replica map differs from the owned replica')
    # A native CR assigned to the Pod can race the process-map observation.
    for kind in ('engines.longhorn.io', 'enginefrontends.longhorn.io', 'replicas.longhorn.io'):
        for obj in lab.get(kind, namespace=NS)['items']:
            if obj.get('status', {}).get('instanceManagerName') != name:
                continue
            if kind != 'replicas.longhorn.io' or obj['metadata']['uid'] != replica['metadata']['uid']:
                raise ValueError('foreign or unaccounted native instance uses the remote V2 Pod')
    proof = {'variant': 'instance-manager', 'fault': 'remote-v2-spdk-instance-manager-pod-loss',
        'volume_uid': journal['volume_uid'], 'replica_uid': replica['metadata']['uid'],
        'replica': replica['metadata']['name'], 'pod': name, 'pod_uid': pod['metadata']['uid'],
        'parent': {'kind': 'InstanceManager', 'name': name, 'uid': manager['metadata']['uid'],
                   'node': manager['spec']['nodeID']}, 'manager_spec': copy.deepcopy(manager['spec']),
        'original_client_node': node, 'image': image}
    return pod, proof


def inject(lab, journal, variant):
    if variant != 'instance-manager':
        raise ValueError('unsupported V2 control fault')
    _, before = boundary(lab, journal)
    pod, checked = boundary(lab, journal)
    if checked != before:
        raise ValueError('V2 Pod loss boundary changed before native DELETE')
    # Persist intent before the DELETE: a lost API response must not bypass
    # recovery-before-cleanup or grant permission to delete a replacement Pod.
    journal['v2_replica_im_loss'] = before
    lab.write_private(lab.DIRECTORY/'longhorn-install-business.json', journal)
    lab.remove(pod)
    print('LONGHORN_V2_REPLICA_IM_LOSS_INJECTED '+json.dumps(before, sort_keys=True), flush=True)
    return before


def recovered(lab, journal, proof, *, emit=True):
    if not lab.healthy_replicas(journal['volume']):
        return False
    topology = lab.replica_topology(journal, 3)
    if not topology:
        return False
    parent = proof['parent']
    manager = lab.get('instancemanagers.longhorn.io', parent['name'], NS)
    if (not manager or manager['metadata']['uid'] != parent['uid'] or manager['spec'] != proof['manager_spec']):
        raise ValueError('V2 instance-manager parent identity or configuration changed during recovery')
    pod = lab.get('pod', parent['name'], NS)
    if pod and not owner(pod, 'InstanceManager', parent['uid']):
        raise ValueError('replacement V2 instance-manager Pod has a foreign parent')
    if (not pod_ready(pod) or pod['metadata']['uid'] == proof['pod_uid'] or
            manager.get('status', {}).get('currentState') != 'running'):
        return False
    if (pod['spec'].get('nodeName') != parent['node'] or
            len(pod['spec']['containers']) != 1 or pod['spec']['containers'][0]['image'] != proof['image']):
        raise ValueError('replacement V2 Pod image or node changed')
    journal['v2_replica_im_recovered'] = True
    lab.write_private(lab.DIRECTORY/'longhorn-install-business.json', journal)
    if emit:
        print('LONGHORN_V2_REPLICA_IM_RECOVERED '+json.dumps({'volume_uid': journal['volume_uid'],
            'parent_uid': parent['uid'], 'pod_uid': pod['metadata']['uid'], 'image': proof['image'],
            'replicas': topology}, sort_keys=True), flush=True)
    return True


def cleanup(lab, original, record=None, **kwargs):
    record = record or lab.DIRECTORY/'longhorn-install-business.json'
    if record.is_symlink():
        raise ValueError('V2 Pod loss refuses a symlink business journal')
    if record.exists():
        journal = json.loads(record.read_text())
        proof = journal.get('v2_replica_im_loss')
        if proof:
            lab.check_business_graph(journal)
            if not journal.get('v2_replica_im_recovered'):
                lab.wait('v2-replica-im-restore-before-cleanup', 1200,
                         lambda: recovered(lab, journal, proof, emit=False))
            # After a successful fault recovery the client may be detached by
            # remount or cleanup. Check shared component identity and health
            # without demanding an attached engine on that normal path.
            manager = lab.get('instancemanagers.longhorn.io', proof['parent']['name'], NS)
            pod = lab.get('pod', proof['pod'], NS)
            if (not manager or manager['metadata']['uid'] != proof['parent']['uid'] or
                    manager['spec'] != proof['manager_spec'] or not pod_ready(pod) or
                    pod['metadata']['uid'] == proof['pod_uid'] or
                    not owner(pod, 'InstanceManager', proof['parent']['uid'])):
                raise ValueError('V2 Pod loss recovery changed before business cleanup; retain journal')
            lab.installation_health()
            print('LONGHORN_V2_REPLICA_IM_FAULT_RESTORED_BEFORE_CLEANUP', flush=True)
    return original(record, **kwargs)


def business(lab, relay_pid):
    original = lab.load_helper
    original_cleanup = lab.cleanup_business
    control = original('longhorn-control-faults.py')
    # Only the per-call module's injection/recovery functions change. Its
    # established fsync ledger, error accounting and remount proof are reused.
    control.inject, control.recovered = inject, recovered
    def load(filename):
        return control if filename == 'longhorn-control-faults.py' else original(filename)
    lab.load_helper = load
    lab.cleanup_business = lambda record=None, **kwargs: cleanup(lab, original_cleanup, record, **kwargs)
    try:
        lab.run_requests(relay_pid, [])
        lab.business('control_failure', relay_pid=relay_pid, fault_variant='instance-manager')
    finally:
        lab.load_helper = original
        lab.cleanup_business = original_cleanup
    print('LONGHORN_V2_REPLICA_IM_LOSS_BUSINESS_PASS', flush=True)


def verify_log(log):
    def one(prefix):
        rows = [json.loads(s.split(' ', 1)[1]) for s in log.splitlines() if s.startswith(prefix+' ')]
        if len(rows) != 1:
            raise ValueError('V2 Pod loss lacks one complete observation: '+prefix)
        return rows[0]
    proof = one('LONGHORN_V2_REPLICA_IM_LOSS_INJECTED')
    recovery = one('LONGHORN_V2_REPLICA_IM_RECOVERED')
    result = one('LONGHORN_CONTROL_FAULT_RESULT')
    ledger = one('LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS')
    remount = one('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED')
    if (proof.get('fault') != 'remote-v2-spdk-instance-manager-pod-loss' or
            not all(proof.get(k) for k in ('volume_uid', 'replica_uid', 'pod_uid')) or
            proof['parent']['node'] == proof['original_client_node'] or
            result.get('variant') != 'instance-manager' or result.get('client_rc') != 0 or
            result.get('recovery_mode') != 'native-component-recreation-original-client' or
            not 0 <= result.get('recovery_s', -1) <= 1200 or not isinstance(result.get('retries'), list)):
        raise ValueError('V2 Pod loss has an invalid fault/recovery or original client result')
    if (recovery.get('volume_uid') != proof['volume_uid'] or
            recovery.get('parent_uid') != proof['parent']['uid'] or
            not recovery.get('pod_uid') or recovery['pod_uid'] == proof['pod_uid'] or
            recovery.get('image') != proof['image'] or len(recovery['replicas']) != 3 or
            len({r['node'] for r in recovery['replicas'].values()}) != 3 or
            len({r['uid'] for r in recovery['replicas'].values() if r['uid']}) != 3):
        raise ValueError('V2 Pod loss lacks actual replacement and healthy three-node replica identities')
    if (not ledger['token'].endswith(':instance-manager') or ledger.get('client') != 'writer' or
            ledger['fresh']['token'] != ledger['token']+':recovered' or
            ledger['fresh'].get('path') != '/data/control-recovered-ledger'):
        raise ValueError('V2 Pod loss ledgers differ from the original client and recovery path')
    faults = _load('longhorn-faults.py')
    control = _load('longhorn-control-faults.py')
    for key in ('confirmed', 'fresh_confirmed'):
        records = ledger['fresh'] if key == 'fresh_confirmed' else ledger
        checked = control.confirmed(records['records'], records['token'], faults)['summary']
        if result[key] != checked or records['summary'] != checked:
            raise ValueError('V2 Pod loss acknowledged prefix or fresh writes differ')
    if remount != {'original': result['confirmed'], 'recovered': result['fresh_confirmed']}:
        raise ValueError('V2 Pod loss remount does not verify both acknowledged ledgers')
    remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    payload = ledger['token'].removesuffix(':instance-manager')
    if (len(remounts) != 1 or not payload or int(remounts[0][0]) != len(payload.encode())*262144 or
            remounts[0][1] != hashlib.sha256(payload.encode()*262144).hexdigest() or
            len(set(remounts[0][2].split(','))) != 3):
        raise ValueError('V2 Pod loss lacks original payload cross-node readback')
    for marker in ('LONGHORN_V2_REPLICA_IM_LOSS_BUSINESS_PASS', 'LONGHORN_CONTROL_FAILURE_BUSINESS_PASS',
                   'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                   'LONGHORN_V2_REPLICA_IM_FAULT_RESTORED_BEFORE_CLEANUP'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('V2 Pod loss lacks business or cleanup marker')
    if re.search(r'^LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)', log, re.M):
        raise ValueError('V2 Pod loss includes a failure marker')
    return result


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'verify', 'cleanup'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log: parser.error('verify requires --log')
        verify_log(args.log.read_text()); return
    if args.mode == 'business' and args.relay_pid <= 0: parser.error('business requires its live relay')
    _, lab = _load('longhorn-v2-install.py').fixture(); lab.require_lease()
    if args.mode == 'cleanup':
        cleanup(lab, lab.cleanup_business, relay_pid=args.relay_pid)
        return
    business(lab, args.relay_pid)


if __name__ == '__main__':
    try: main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-control-fault-', suffix='.txt', dir='/var/tmp', delete=False) as stream:
            traceback.print_exc(file=stream)
            print('LONGHORN_V2_CONTROL_FAULT_FAIL private_diagnostic='+stream.name, file=sys.stderr)
        sys.exit(1)
