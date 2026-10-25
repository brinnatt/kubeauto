"""UID-scoped V1 engine, instance-manager and manager failure probes.

Bare application Pods use Longhorn's documented manual recreation path after
an engine crash. This gate makes no automatic controller-restart claim.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid


NAMESPACE = 'longhorn-system'


def instance_manager_image(lab):
    tag = json.loads((lab.BASE/'roles/longhorn/files/images.json').read_text())['1.12.1']['longhorn-instance-manager']
    return '/longhorn-instance-manager:'+tag


def ready(pod):
    return bool(pod and not pod['metadata'].get('deletionTimestamp') and
                pod.get('status', {}).get('phase') == 'Running' and
                any(c.get('type') == 'Ready' and c.get('status') == 'True'
                    for c in pod.get('status', {}).get('conditions', [])))


def controlled(pod, kind, uid):
    return any(ref.get('kind') == kind and ref.get('uid') == uid and ref.get('controller')
               for ref in pod.get('metadata', {}).get('ownerReferences', []))


def source(lab, journal):
    """Resolve the current engine's native UUID and its full owner chain."""
    ns = lab.get('namespace', NAMESPACE)
    volume = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
    nodes = {'longhorn-node-' + ip.rsplit('.', 1)[1] for ip in lab.WORKERS}
    if (not ns or ns['metadata']['uid'] != lab.owner()['namespace_uid'] or not volume or
            volume['metadata']['uid'] != journal['volume_uid'] or
            volume['spec'].get('dataEngine') != 'v1' or volume['spec'].get('encrypted') or
            volume['spec'].get('accessMode') != 'rwo' or volume['status'].get('state') != 'attached' or
            volume['status'].get('currentNodeID') not in nodes or not lab.replica_topology(journal, 3)):
        raise ValueError('control fault requires its healthy owned V1 RWO volume')
    return engine_identity(lab, journal, volume)


def engine_identity(lab, journal, volume):
    """Resolve the running owner chain after the caller checks volume health."""
    engines = [e for e in lab.get('engines.longhorn.io', namespace=NAMESPACE)['items']
               if e['spec'].get('volumeName') == journal['volume']]
    if len(engines) != 1:
        raise ValueError('control fault requires one original engine')
    engine = engines[0]
    if (engine['metadata'].get('deletionTimestamp') or engine['spec'].get('nodeID') != volume['status']['currentNodeID'] or
            engine.get('status', {}).get('currentState') != 'running' or
            not any(ref.get('kind') == 'Volume' and ref.get('uid') == journal['volume_uid']
                    for ref in engine['metadata'].get('ownerReferences', []))):
        raise ValueError('control fault engine identity changed')
    manager_name = engine['status'].get('instanceManagerName')
    manager = lab.get('instancemanagers.longhorn.io', manager_name, NAMESPACE) if manager_name else None
    pod = lab.get('pod', manager_name, NAMESPACE) if manager_name else None
    if (not manager or manager['spec'].get('nodeID') != engine['spec']['nodeID'] or
            manager['spec'].get('dataEngine') != 'v1' or manager['status'].get('currentState') != 'running' or
            not ready(pod) or pod['spec'].get('nodeName') != manager['spec']['nodeID'] or
            not controlled(pod, 'InstanceManager', manager['metadata']['uid']) or
            len(pod['spec']['containers']) != 1 or pod['spec']['containers'][0]['name'] != 'instance-manager' or
            not pod['spec']['containers'][0]['image'].endswith(instance_manager_image(lab)) or
            not any(v.get('secret', {}).get('secretName') == 'longhorn-grpc-tls' for v in pod['spec'].get('volumes', []))):
        raise ValueError('control fault engine instance-manager identity changed')
    process = json.loads(lab.kubectl('-n', NAMESPACE, 'exec', manager_name, '-c', 'instance-manager', '--',
        'longhorn-instance-manager', '--tls-dir', '/tls-files/', 'process', 'get', '--name', engine['metadata']['name'],
        timeout=30).stdout)
    native = process.get('spec', {}); status = process.get('status', {})
    try:
        uuid.UUID(status.get('uuid', ''))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('control fault engine has no native UUID') from error
    if (native.get('name') != engine['metadata']['name'] or status.get('state') != 'running' or
            not native.get('binary', '').endswith('/longhorn') or
            native.get('args', [])[:4] != ['--engine-instance-name', engine['metadata']['name'],
                                            'controller', journal['volume']]):
        raise ValueError('control fault native engine differs from its volume')
    return {'name': engine['metadata']['name'], 'uid': engine['metadata']['uid'], 'uuid': status['uuid'],
            'node': engine['spec']['nodeID'], 'manager': manager_name,
            'manager_uid': manager['metadata']['uid'], 'pod_uid': pod['metadata']['uid']}


def fault_pod(lab, journal, variant, engine):
    # A manager or instance-manager is shared infrastructure. Refuse a cluster
    # containing any other volume before touching its processes.
    volumes = lab.get('volumes.longhorn.io', namespace=NAMESPACE)['items']
    if [(v['metadata']['name'], v['metadata']['uid']) for v in volumes] != [(journal['volume'], journal['volume_uid'])]:
        raise ValueError('shared control fault refuses foreign volume consumers')
    if variant == 'manager':
        parent = lab.get('daemonset', 'longhorn-manager', NAMESPACE)
        if not parent or parent['metadata'].get('labels', {}).get('app.kubernetes.io/instance') != 'longhorn':
            raise ValueError('control fault manager DaemonSet ownership differs')
        parent_kind = 'DaemonSet'
        node = engine['node']
        candidates = [p for p in lab.get('pods', namespace=NAMESPACE)['items']
                      if p['spec'].get('nodeName') == node and p['metadata'].get('labels', {}).get('app') == 'longhorn-manager']
    elif variant == 'instance-manager':
        # Lose a replica-hosting instance-manager on another worker. The engine
        # variant independently exercises loss of the active iSCSI frontend.
        replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NAMESPACE)['items']
                    if r['spec'].get('volumeName') == journal['volume'] and r['spec'].get('nodeID') != engine['node']]
        if len(replicas) != 2:
            raise ValueError('control fault lacks two remote replicas')
        replica = sorted(replicas, key=lambda r: r['metadata']['name'])[0]
        parent = lab.get('instancemanagers.longhorn.io', replica['status']['instanceManagerName'], NAMESPACE)
        if (not parent or parent['spec'].get('dataEngine') != 'v1' or
                parent['spec'].get('nodeID') != replica['spec']['nodeID'] or parent['status'].get('currentState') != 'running'):
            raise ValueError('control fault replica instance-manager differs')
        parent_kind = 'InstanceManager'; node = parent['spec']['nodeID']
        candidate = lab.get('pod', parent['metadata']['name'], NAMESPACE)
        candidates = [candidate] if candidate else []
    else:
        raise ValueError('unsupported shared control fault')
    if len(candidates) != 1 or not ready(candidates[0]) or candidates[0]['spec'].get('nodeName') != node or not controlled(
            candidates[0], parent_kind, parent['metadata']['uid']):
        raise ValueError('control fault Pod ownership or readiness differs')
    pod = candidates[0]
    if variant == 'instance-manager':
        if (len(pod['spec']['containers']) != 1 or pod['spec']['containers'][0]['name'] != 'instance-manager' or
                not pod['spec']['containers'][0]['image'].endswith(instance_manager_image(lab)) or
                not any(v.get('secret', {}).get('secretName') == 'longhorn-grpc-tls' for v in pod['spec'].get('volumes', []))):
            raise ValueError('shared control fault requires the pinned mTLS instance-manager')
        allowed = {}
        for resource in ('engines.longhorn.io', 'replicas.longhorn.io'):
            for instance in lab.get(resource, namespace=NAMESPACE)['items']:
                if (instance['spec'].get('volumeName') == journal['volume'] and instance['spec'].get('nodeID') == node and
                        instance.get('status', {}).get('instanceManagerName') == parent['metadata']['name'] and
                        any(ref.get('kind') == 'Volume' and ref.get('uid') == journal['volume_uid']
                            for ref in instance['metadata'].get('ownerReferences', []))):
                    allowed[instance['metadata']['name']] = instance
        processes = json.loads(lab.kubectl('-n', NAMESPACE, 'exec', pod['metadata']['name'], '-c', 'instance-manager', '--',
            'longhorn-instance-manager', '--tls-dir', '/tls-files/', 'process', 'list', timeout=30).stdout)
        if not allowed or set(processes) != set(allowed) or any(
                p.get('spec', {}).get('name') != name or p.get('status', {}).get('state') != 'running'
                for name, p in processes.items()):
            raise ValueError('shared control fault refuses foreign or unready native processes')
    return pod, {'kind': parent_kind, 'name': parent['metadata']['name'], 'uid': parent['metadata']['uid'], 'node': node}


def inject(lab, journal, variant):
    before = source(lab, journal)
    if variant == 'engine':
        if source(lab, journal) != before:
            raise ValueError('control fault engine changed before deletion')
        response = json.loads(lab.kubectl('-n', NAMESPACE, 'exec', before['manager'], '-c', 'instance-manager', '--',
            'longhorn-instance-manager', '--tls-dir', '/tls-files/', 'process', 'delete', '--name', before['name'],
            '--uuid', before['uuid'], timeout=30).stdout)
        if (response.get('spec', {}).get('name') != before['name'] or
                response.get('status', {}).get('uuid') != before['uuid'] or response.get('deleted') is not True):
            raise ValueError('control fault did not confirm native engine UUID deletion')
        proof = {'variant': variant, 'engine': before}
    else:
        pod, parent = fault_pod(lab, journal, variant, before)
        checked, checked_parent = fault_pod(lab, journal, variant, before)
        if checked['metadata']['uid'] != pod['metadata']['uid'] or checked_parent != parent:
            raise ValueError('shared control fault boundary changed before deletion')
        current = lab.get('pod', pod['metadata']['name'], NAMESPACE)
        if not current or current['metadata']['uid'] != pod['metadata']['uid']:
            raise ValueError('control fault Pod was replaced before deletion')
        # remove uses raw DELETE with UID and resourceVersion preconditions.
        lab.remove(current)
        proof = {'variant': variant, 'engine': before, 'pod': pod['metadata']['name'],
                 'pod_uid': pod['metadata']['uid'], 'parent': parent}
    print('LONGHORN_CONTROL_FAULT_INJECTED ' + json.dumps(proof, sort_keys=True), flush=True)
    return proof


def recovered(lab, journal, proof):
    if not lab.replica_topology(journal, 3):
        return False
    if proof['variant'] == 'engine':
        return source(lab, journal)['uuid'] != proof['engine']['uuid']
    parent = proof['parent']
    kind = 'daemonset' if parent['kind'] == 'DaemonSet' else 'instancemanagers.longhorn.io'
    obj = lab.get(kind, parent['name'], NAMESPACE)
    if not obj or obj['metadata']['uid'] != parent['uid']:
        raise ValueError('control fault parent was replaced during recovery')
    pods = [p for p in lab.get('pods', namespace=NAMESPACE)['items'] if p['spec'].get('nodeName') == parent['node'] and
            controlled(p, parent['kind'], parent['uid'])]
    return len(pods) == 1 and pods[0]['metadata']['uid'] != proof['pod_uid'] and ready(pods[0])


def ledger_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines(keepends=True)
            if line.startswith('{') and line.endswith('\n')]


def confirmed(rows, token, faults):
    records = [row for row in rows if row.get('type') == 'ack']
    if len(records) < 4:
        raise ValueError('control fault has too few acknowledged writes')
    digest = hashlib.sha256()
    for sequence, row in enumerate(records):
        data = faults.ledger_block(token, sequence)
        if row != {'type': 'ack', 'sequence': sequence, 'sha256': hashlib.sha256(data).hexdigest()}:
            raise ValueError('control fault acknowledgement sequence differs')
        digest.update(data)
    return {'token': token, 'records': records,
            'summary': {'confirmed': len(records), 'verified_bytes': len(records)*faults.BLOCK, 'sha256': digest.hexdigest()}}


def verify(lab, journal, create, variant, *, read_only_recovery=None):
    if variant not in ('engine', 'instance-manager', 'manager'):
        raise ValueError('unknown control fault variant')
    faults = lab.load_helper('longhorn-faults.py')
    token = lab.owner()['run_id'] + ':' + variant
    observations = Path(tempfile.mkdtemp(prefix='kubeauto-longhorn-control-fault-', dir='/var/tmp'))
    log = observations/'ledger.log'; success = False; client = 'writer'
    with log.open('w') as stream:
        process = subprocess.Popen([str(lab.BASE/'kube-bin/kubectl'), '--kubeconfig=' + str(lab.DIRECTORY/'kubectl.kubeconfig'),
            '-n', journal['namespace'], 'exec', '-i', client, '--', 'python3', '-u', '-c', faults.ledger_program(token)],
            stdin=subprocess.PIPE, stdout=stream, stderr=stream, text=True)
        def recreate_client(proof):
            # A bare Pod has no controller to restart it after an unexpected
            # detach. Fence its exact old UID before creating a new client.
            old = lab.get('pod', 'writer', journal['namespace'])
            if (not old or old['metadata']['uid'] != journal['objects']['Pod/writer'] or
                    old['metadata'].get('ownerReferences')):
                raise ValueError('control fault original bare application Pod changed')
            lab.remove(old)
            lab.wait('control-fault-old-writer-gone', 180, lambda: lab.get('pod', 'writer', journal['namespace']) is None)
            lab.wait('control-fault-old-client-exited', 30, lambda: process.poll() is not None)
            name = 'recovered-writer'
            recreated = create(lab.client_pod(journal['namespace'], name, 'data', proof['engine']['node']))
            def client_ready():
                current = lab.get('pod', name, journal['namespace'])
                if not current or current['metadata']['uid'] != recreated['metadata']['uid']:
                    raise ValueError('recreated control fault client was replaced')
                return ready(current)
            lab.wait('control-fault-recreated-writer-ready', 600, client_ready)
            return name, recreated['metadata']['uid']
        try:
            def started():
                if process.poll() is not None:
                    raise ValueError('control fault client exited before injection')
                return len([r for r in ledger_rows(log) if r.get('type') == 'ack']) >= 4
            lab.wait('control-fault-writer-started', 60, started)
            started_at = time.monotonic(); proof = inject(lab, journal, variant)
            readonly = None
            if variant == 'engine':
                # Official bare-Pod recovery: delete the original application
                # Pod, wait for fencing by actual deletion, recreate a new name.
                client, _ = recreate_client(proof)
                recovery_mode = 'documented-bare-pod-recreation'
            else:
                recovery_mode = 'native-component-recreation-original-client'
            lab.wait('control-fault-native-recovered', 1200, lambda: recovered(lab, journal, proof))
            if client == 'writer':
                count = len([r for r in ledger_rows(log) if r.get('type') == 'ack'])
                def resumed():
                    nonlocal readonly
                    if process.poll() is not None:
                        raise ValueError('control fault client stopped before post-recovery writes')
                    current = ledger_rows(log)
                    if len([r for r in current if r.get('type') == 'ack']) >= count + 4:
                        return True
                    if read_only_recovery is not None:
                        readonly = read_only_recovery(lab, journal, proof, current)
                    return bool(readonly)
                lab.wait('control-fault-post-recovery-writes', 60, resumed)
                if readonly:
                    client, uid = recreate_client(proof)
                    readonly = dict(readonly, replacement_pod_uid=uid)
                    recovery_mode = 'documented-read-only-bare-pod-recreation'
                else:
                    process.stdin.write('stop\n'); process.stdin.flush()
                    lab.wait('control-fault-client-completed', 60, lambda: process.poll() is not None)
                    completed = [r for r in ledger_rows(log) if r.get('type') == 'complete']
                    if process.returncode or len(completed) != 1:
                        raise ValueError('control fault client did not complete normally')
            recovery_s = time.monotonic() - started_at
            rows = ledger_rows(log); result = confirmed(rows, token, faults)
            faults.verify_client_ledger(lab, journal, client, result)
            # Confirm fresh writes through the recovered frontend as well as
            # the entire pre-fault acknowledged prefix. Never overwrite it.
            fresh = token + ':recovered'
            code = faults.ledger_program(fresh, duration=5).replace('/data/fault-ledger', '/data/control-recovered-ledger')
            # A finite standalone workload does not use the stdin stop channel.
            code = code.replace('stop=ledger_stop_requested', 'stop=None')
            output = lab.kubectl('-n', journal['namespace'], 'exec', client, '--', 'python3', '-u', '-c', code, timeout=30)
            after = [json.loads(line) for line in output.stdout.splitlines()]
            fresh_proof = confirmed(after, fresh, faults)
            fresh_proof['path'] = '/data/control-recovered-ledger'
            result.update(client=client, fresh=fresh_proof)
            result_row = {'variant': variant, 'recovery_mode': recovery_mode,
                'recovery_s': round(recovery_s, 3), 'client_rc': process.returncode,
                'retries': [r for r in rows if r.get('type') == 'retry'],
                'confirmed': result['summary'], 'fresh_confirmed': fresh_proof['summary']}
            if readonly:
                result_row['read_only_recovery'] = readonly
            print('LONGHORN_CONTROL_FAULT_RESULT ' + json.dumps(result_row, sort_keys=True), flush=True)
            print('LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS ' + json.dumps(result, sort_keys=True), flush=True)
            success = True
            return result
        finally:
            if process.stdin:
                try: process.stdin.close()
                except BrokenPipeError: pass
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=10)
            if success: shutil.rmtree(observations)
            else: print('LONGHORN_PRIVATE_DIAGNOSTIC directory=' + str(observations), flush=True)


def verify_remount(lab, journal, client, proof):
    faults = lab.load_helper('longhorn-faults.py')
    faults.verify_client_ledger(lab, journal, client, proof)
    # Reuse the same independent verifier for the separately confirmed writes.
    from types import SimpleNamespace
    def kubectl(*args, **kwargs):
        args = tuple(arg.replace('/data/fault-ledger', proof['fresh']['path']) if isinstance(arg, str) else arg for arg in args)
        return lab.kubectl(*args, **kwargs)
    faults.verify_client_ledger(SimpleNamespace(kubectl=kubectl), journal, client, proof['fresh'])
    print('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED ' + json.dumps(
        {'original': proof['summary'], 'recovered': proof['fresh']['summary']}, sort_keys=True), flush=True)
