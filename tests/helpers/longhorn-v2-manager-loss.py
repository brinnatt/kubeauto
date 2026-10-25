#!/usr/bin/env python3
"""V2 manager Pod replacement with original data-plane identities and ledgers.

Only the control-plane DaemonSet Pod is deleted, with native UID/RV guards.
This does not simulate loss of its host, the SPDK target or an engine.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile

NS = 'longhorn-system'
RECORD = 'longhorn-install-business.json'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime(lab, journal):
    """Resolve actual native consumers; a Ready manager alone is insufficient."""
    common = load('longhorn-v2-control-faults.py')
    # The existing boundary rejects foreign volumes and verifies the pinned,
    # TLS-enabled V2 AIO runtime and healthy original three raw disks.
    _, source = common.boundary(lab, journal)
    managers = {}
    tag = json.loads((lab.BASE/'roles/longhorn/files/images.json').read_text())['1.12.1']['longhorn-instance-manager']
    for obj in lab.get('instancemanagers.longhorn.io', namespace=NS)['items']:
        name = obj['metadata']['name']
        pod = lab.get('pod', name, NS)
        if (obj['spec'].get('dataEngine') != 'v2' or obj['spec'].get('type') != 'aio' or
                obj.get('status', {}).get('currentState') != 'running' or not common.pod_ready(pod) or
                not common.owner(pod, 'InstanceManager', obj['metadata']['uid'])):
            raise ValueError('manager loss requires healthy original V2 instance managers')
        containers = pod['spec']['containers']
        if (not obj['spec'].get('image', '').endswith('/longhorn-instance-manager:'+tag) or
                len(containers) != 1 or containers[0].get('name') != 'instance-manager' or
                containers[0].get('image') != obj['spec']['image'] or
                '--spdk-enabled' not in containers[0].get('args', []) or
                not any(v.get('secret', {}).get('secretName') == 'longhorn-grpc-tls'
                        for v in pod['spec'].get('volumes', []))):
            raise ValueError('manager loss requires pinned SPDK Pods with TLS')
        managers[name] = {'uid': obj['metadata']['uid'], 'spec': copy.deepcopy(obj['spec']),
                          'pod_uid': pod['metadata']['uid']}
    if len(managers) != 3 or len({m['spec']['nodeID'] for m in managers.values()}) != 3:
        raise ValueError('manager loss requires exactly three native V2 instance managers')
    instances = {}
    for kind, count in (('engines.longhorn.io', 1), ('enginefrontends.longhorn.io', 1), ('replicas.longhorn.io', 3)):
        objects = lab.get(kind, namespace=NS)['items']
        if len(objects) != count:
            raise ValueError('manager loss refuses missing or foreign native instances')
        rows = {}
        for obj in objects:
            manager = obj.get('status', {}).get('instanceManagerName')
            if (obj['spec'].get('volumeName') != journal['volume'] or
                    not common.owner(obj, 'Volume', journal['volume_uid'], controller=False) or
                    obj.get('status', {}).get('currentState') != 'running' or manager not in managers):
                raise ValueError('manager loss native consumer ownership/readiness differs')
            rows[obj['metadata']['name']] = {'uid': obj['metadata']['uid'],
                'node': obj['spec'].get('nodeID'), 'disk': obj['spec'].get('diskID', ''), 'manager': manager}
        instances[kind] = rows
    return {'managers': managers, 'instances': instances, 'replicas': lab.replica_topology(journal, 3),
            'volume_uid': journal['volume_uid'], 'client_node': source['original_client_node']}


def manager_pod(lab, parent, node):
    common = load('longhorn-v2-control-faults.py')
    ds = lab.get('daemonset', 'longhorn-manager', NS)
    if (not ds or ds['metadata']['uid'] != parent['uid'] or ds['spec'] != parent['spec']):
        raise ValueError('manager loss DaemonSet identity/configuration changed')
    pods = [p for p in lab.get('pods', namespace=NS)['items']
            if p['spec'].get('nodeName') == node and common.owner(p, 'DaemonSet', parent['uid'])]
    if len(pods) != 1:
        return None
    pod = pods[0]
    if not common.pod_ready(pod):
        return None
    if pod['metadata'].get('labels', {}).get('app') != 'longhorn-manager':
        raise ValueError('manager loss replacement has a foreign component label')
    containers = [c for c in pod['spec']['containers'] if c['name'] == 'longhorn-manager']
    tag = json.loads((lab.BASE/'roles/longhorn/files/images.json').read_text())['1.12.1']['longhorn-manager']
    if len(containers) != 1 or not containers[0]['image'].endswith('/longhorn-manager:'+tag):
        raise ValueError('manager loss requires the pinned manager image')
    return pod


def inject(lab, journal, variant):
    if variant != 'manager':
        raise ValueError('unsupported V2 manager fault')
    original = runtime(lab, journal)
    ds = lab.get('daemonset', 'longhorn-manager', NS)
    if not ds or ds['metadata'].get('labels', {}).get('app.kubernetes.io/instance') != 'longhorn':
        raise ValueError('manager loss requires the owned Longhorn DaemonSet')
    parent = {'uid': ds['metadata']['uid'], 'spec': copy.deepcopy(ds['spec'])}
    pod = manager_pod(lab, parent, original['client_node'])
    if not pod:
        raise ValueError('manager loss original Pod is not uniquely Ready')
    if runtime(lab, journal) != original:
        raise ValueError('manager loss data-plane boundary changed before DELETE')
    checked = manager_pod(lab, parent, original['client_node'])
    if not checked or checked['metadata']['uid'] != pod['metadata']['uid']:
        raise ValueError('manager loss original Pod changed before DELETE')
    proof = {'fault': 'v2-manager-pod-delete', 'pod': pod['metadata']['name'],
             'pod_uid': pod['metadata']['uid'], 'parent': parent, 'runtime': original}
    journal['v2_manager_loss'] = proof
    lab.write_private(lab.DIRECTORY/RECORD, journal)
    lab.remove(checked)
    print('LONGHORN_V2_MANAGER_LOSS_INJECTED '+json.dumps(proof, sort_keys=True), flush=True)
    return proof


def component_restored(lab, proof):
    pod = manager_pod(lab, proof['parent'], proof['runtime']['client_node'])
    if not pod or pod['metadata']['uid'] == proof['pod_uid']:
        return None
    common = load('longhorn-v2-control-faults.py')
    managers = lab.get('instancemanagers.longhorn.io', namespace=NS)['items']
    if {m['metadata']['name'] for m in managers} != set(proof['runtime']['managers']):
        raise ValueError('manager loss changed the native instance-manager set')
    for obj in managers:
        name = obj['metadata']['name']
        old = proof['runtime']['managers'][name]
        im_pod = lab.get('pod', name, NS)
        if (obj['metadata']['uid'] != old['uid'] or obj['spec'] != old['spec'] or
                not im_pod or im_pod['metadata']['uid'] != old['pod_uid'] or
                not common.owner(im_pod, 'InstanceManager', old['uid'])):
            raise ValueError('manager loss unexpectedly replaced a data-plane component')
        if not common.pod_ready(im_pod) or obj.get('status', {}).get('currentState') != 'running':
            return None
    return pod


def recovered(lab, journal, proof, *, emit=True):
    pod = component_restored(lab, proof)
    if not pod or not lab.healthy_replicas(journal['volume']):
        return False
    observed = runtime(lab, journal)
    if observed != proof['runtime']:
        raise ValueError('manager replacement changed original native data-plane identities')
    journal['v2_manager_recovered'] = True
    lab.write_private(lab.DIRECTORY/RECORD, journal)
    if emit:
        print('LONGHORN_V2_MANAGER_LOSS_RECOVERED '+json.dumps({
            'pod_uid': pod['metadata']['uid'], 'parent_uid': proof['parent']['uid'],
            'runtime': observed}, sort_keys=True), flush=True)
    return True


def diagnostics(lab, journal):
    """Preserve the owning failure before recovery can change native state."""
    directory = Path(tempfile.mkdtemp(prefix='longhorn-v2-manager-failure-', dir=lab.DIRECTORY))
    lab.write_private(directory/'first-failure.json', {'exception_type': sys.exc_info()[0].__name__,
                      'journal': journal})
    for name in ('config.yml', 'yml/longhorn/input.json'):
        source = lab.DIRECTORY/name
        if source.is_file() and not source.is_symlink():
            target = directory/name.replace('/', '-')
            target.write_bytes(source.read_bytes())
            target.chmod(0o600)
    requests = {
        'native': ('-n', NS, 'get', 'volumes.longhorn.io,engines.longhorn.io,enginefrontends.longhorn.io,replicas.longhorn.io,instancemanagers.longhorn.io', '-o', 'json'),
        'manager-pods': ('-n', NS, 'describe', 'pods', '-l', 'app=longhorn-manager'),
        'events': ('-n', NS, 'get', 'events', '--sort-by=.lastTimestamp'),
        'manager-logs': ('-n', NS, 'logs', '-l', 'app=longhorn-manager', '--all-containers', '--prefix', '--tail=100'),
        'business-pods': ('-n', journal['namespace'], 'describe', 'pods'),
    }
    for name, args in requests.items():
        try:
            result = lab.kubectl(*args, check=False, timeout=10)
            row = {'rc': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
        except Exception as error:
            row = {'collection_error': type(error).__name__}
        lab.write_private(directory/(name+'.json'), row)
    print('LONGHORN_PRIVATE_DIAGNOSTIC directory='+str(directory), flush=True)


def cleanup(lab, original, record=None, **kwargs):
    record = record or lab.DIRECTORY/RECORD
    if record.is_symlink():
        raise ValueError('manager loss refuses a symlink journal')
    if record.exists():
        journal = json.loads(record.read_text())
        if sys.exc_info()[0] is not None:
            try:
                diagnostics(lab, journal)
            except Exception as error:
                print('LONGHORN_PRIVATE_DIAGNOSTIC_UNAVAILABLE type='+type(error).__name__, flush=True)
        proof = journal.get('v2_manager_loss')
        if proof:
            lab.check_business_graph(journal)
            if not journal.get('v2_manager_recovered'):
                lab.wait('v2-manager-restored-before-cleanup', 1200,
                         lambda: recovered(lab, journal, proof, emit=False))
            if not component_restored(lab, proof):
                raise ValueError('manager loss recovery changed before cleanup; retain journal')
            lab.installation_health()
            print('LONGHORN_V2_MANAGER_RESTORED_BEFORE_CLEANUP', flush=True)
    return original(record, **kwargs)


def business(lab, relay_pid):
    original, original_cleanup = lab.load_helper, lab.cleanup_business
    control = original('longhorn-control-faults.py')
    control.inject, control.recovered = inject, recovered
    lab.load_helper = lambda name: control if name == 'longhorn-control-faults.py' else original(name)
    lab.cleanup_business = lambda record=None, **kw: cleanup(lab, original_cleanup, record, **kw)
    try:
        lab.run_requests(relay_pid, [])
        lab.business('control_failure', relay_pid=relay_pid, fault_variant='manager')
    finally:
        lab.load_helper, lab.cleanup_business = original, original_cleanup
    print('LONGHORN_V2_MANAGER_LOSS_BUSINESS_PASS', flush=True)


def verify_log(log):
    def one(prefix):
        rows = [json.loads(s.split(' ', 1)[1]) for s in log.splitlines() if s.startswith(prefix+' ')]
        if len(rows) != 1:
            raise ValueError('manager loss lacks one observation: '+prefix)
        return rows[0]
    proof = one('LONGHORN_V2_MANAGER_LOSS_INJECTED')
    recovery = one('LONGHORN_V2_MANAGER_LOSS_RECOVERED')
    result = one('LONGHORN_CONTROL_FAULT_RESULT')
    ledger = one('LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS')
    remount = one('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED')
    native = proof['runtime']
    if (len({m.get('uid') for m in native['managers'].values() if m.get('uid')}) != 3 or
            len({m.get('pod_uid') for m in native['managers'].values() if m.get('pod_uid')}) != 3 or
            len({m['spec']['nodeID'] for m in native['managers'].values()}) != 3 or
            len({r.get('uid') for r in native['replicas'].values() if r.get('uid')}) != 3):
        raise ValueError('manager loss lacks original instance-manager and replica identities')
    for kind, count in (('engines.longhorn.io', 1), ('enginefrontends.longhorn.io', 1), ('replicas.longhorn.io', 3)):
        instances = native['instances'].get(kind, {})
        if (len(instances) != count or len({r.get('uid') for r in instances.values() if r.get('uid')}) != count or
                any(r.get('manager') not in native['managers'] for r in instances.values())):
            raise ValueError('manager loss lacks original native instance identities')
    if (proof.get('fault') != 'v2-manager-pod-delete' or not proof.get('pod_uid') or
            not recovery.get('pod_uid') or recovery['pod_uid'] == proof['pod_uid'] or
            not proof['parent'].get('uid') or recovery.get('parent_uid') != proof['parent']['uid'] or
            recovery.get('runtime') != proof['runtime'] or not proof['runtime'].get('volume_uid') or
            len(proof['runtime']['managers']) != 3 or len(proof['runtime']['replicas']) != 3 or
            len({r['node'] for r in proof['runtime']['replicas'].values()}) != 3 or
            result.get('variant') != 'manager' or result.get('client_rc') != 0 or
            result.get('recovery_mode') != 'native-component-recreation-original-client' or
            not 0 <= result.get('recovery_s', -1) <= 1200 or not isinstance(result.get('retries'), list)):
        raise ValueError('manager loss lacks replacement, unchanged runtime or original-client recovery')
    if (not ledger['token'].endswith(':manager') or ledger.get('client') != 'writer' or
            ledger['fresh']['token'] != ledger['token']+':recovered' or
            ledger['fresh'].get('path') != '/data/control-recovered-ledger'):
        raise ValueError('manager loss acknowledgement identity differs')
    faults, control = load('longhorn-faults.py'), load('longhorn-control-faults.py')
    for key, records in (('confirmed', ledger), ('fresh_confirmed', ledger['fresh'])):
        checked = control.confirmed(records['records'], records['token'], faults)['summary']
        if result[key] != checked or records['summary'] != checked:
            raise ValueError('manager loss acknowledged writes differ')
    if remount != {'original': result['confirmed'], 'recovered': result['fresh_confirmed']}:
        raise ValueError('manager loss lacks both cross-node ledger readbacks')
    matches = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    token = ledger['token'].removesuffix(':manager')
    if (len(matches) != 1 or not token or int(matches[0][0]) != len(token.encode())*262144 or
            matches[0][1] != hashlib.sha256(token.encode()*262144).hexdigest() or
            set(matches[0][2].split(',')) != {r['node'] for r in proof['runtime']['replicas'].values()}):
        raise ValueError('manager loss lacks original payload cross-node readback')
    for marker in ('LONGHORN_V2_MANAGER_LOSS_BUSINESS_PASS', 'LONGHORN_CONTROL_FAILURE_BUSINESS_PASS',
                   'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', 'LONGHORN_V2_MANAGER_RESTORED_BEFORE_CLEANUP'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('manager loss lacks a unique business/cleanup marker')
    if re.search(r'^LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)', log, re.M):
        raise ValueError('manager loss includes a failure marker')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'cleanup', 'verify'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log:
            parser.error('verify requires --log')
        verify_log(args.log.read_text())
        return
    _, lab = load('longhorn-v2-install.py').fixture()
    lab.require_lease()
    if args.mode == 'business':
        if args.relay_pid <= 0:
            parser.error('business requires its runner-owned relay')
        business(lab, args.relay_pid)
    else:
        cleanup(lab, lab.cleanup_business, relay_pid=args.relay_pid)


if __name__ == '__main__':
    main()
