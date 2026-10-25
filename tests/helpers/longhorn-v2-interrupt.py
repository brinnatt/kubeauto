#!/usr/bin/env python3
"""Product-only V2 polling/interrupt roundtrip on the owned AIO installation.

The journal survives interruption. Recovery detaches only its own clients and
restores the original product configuration before normal business cleanup.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile

import yaml

NS = 'longhorn-system'
SETTING = 'data-engine-interrupt-mode-enabled'
RECORD = 'longhorn-v2-interrupt.json'
BUSINESS = 'longhorn-install-business.json'
REFUSAL = 'V2 resource-setting changes require all V2 volumes detached: '+SETTING


def ready(pod):
    return bool(pod and not pod['metadata'].get('deletionTimestamp') and
                pod.get('status', {}).get('phase') == 'Running' and any(
                    c.get('type') == 'Ready' and c.get('status') == 'True'
                    for c in pod.get('status', {}).get('conditions', [])))


def native_identity(lab, original=None):
    namespace = lab.get('namespace', NS)
    if not namespace or namespace['metadata']['uid'] != lab.owner()['namespace_uid']:
        raise ValueError('interrupt mode installation namespace changed')
    setting = lab.get('settings.longhorn.io', SETTING, NS)
    if not setting or (original and setting['metadata']['uid'] != original['setting_uid']):
        raise ValueError('interrupt mode Setting was replaced')
    expected_nodes = {'longhorn-node-'+n.rsplit('.', 1)[1] for n in lab.WORKERS}
    managers = lab.get('instancemanagers.longhorn.io', namespace=NS)['items']
    if (len(managers) != 3 or {m['spec'].get('nodeID') for m in managers} != expected_nodes or
            any(m['spec'].get('dataEngine') != 'v2' or m['spec'].get('type') != 'aio' for m in managers)):
        raise ValueError('interrupt mode requires the original three V2 instance managers')
    for manager in managers:
        name = manager['metadata']['name']
        identity = {'uid': manager['metadata']['uid'], 'spec': manager['spec']}
        if original and (name not in original['managers'] or any(
                original['managers'][name][key] != value for key, value in identity.items())):
            raise ValueError('interrupt mode instance-manager identity/configuration changed')
    return setting, managers


def components(lab, enabled, original=None):
    """Observe actual flags and native status; never synthesize runtime state."""
    setting, managers = native_identity(lab, original)
    if json.loads(setting['value']).get('v2') != str(enabled).lower():
        return None
    result = {'setting_uid': setting['metadata']['uid'], 'setting_value': setting['value'], 'managers': {}}
    for manager in managers:
        name = manager['metadata']['name']
        identity = {'uid': manager['metadata']['uid'], 'spec': manager['spec']}
        pod = lab.get('pod', name, NS)
        if not ready(pod) or manager.get('status', {}).get('currentState') != 'running':
            return None
        if (pod['spec'].get('nodeName') != manager['spec']['nodeID'] or not any(
                r.get('kind') == 'InstanceManager' and r.get('uid') == identity['uid'] and r.get('controller') is True
                for r in pod['metadata'].get('ownerReferences', []))):
            raise ValueError('interrupt mode Pod owner or placement changed')
        containers = pod['spec']['containers']
        if (len(containers) != 1 or containers[0].get('name') != 'instance-manager' or
                containers[0].get('image') != manager['spec']['image'] or
                '--spdk-enabled' not in containers[0].get('args', []) or
                not any(v.get('secret', {}).get('secretName') == 'longhorn-grpc-tls'
                        for v in pod['spec'].get('volumes', []))):
            raise ValueError('interrupt mode Pod image or TLS changed')
        args = containers[0].get('args', [])
        if ('--spdk-interrupt-mode' in args) != enabled or manager.get('status', {}).get(
                'dataEngineStatus', {}).get('v2', {}).get('interruptModeEnabled') != str(enabled).lower():
            return None
        result['managers'][name] = dict(copy.deepcopy(identity), pod_uid=pod['metadata']['uid'], args=args)
    return result


def only_business(lab, journal):
    lab.check_business_graph(journal)
    volumes = lab.get('volumes.longhorn.io', namespace=NS)['items']
    if (len(volumes) != 1 or volumes[0]['metadata']['uid'] != journal['volume_uid'] or
            volumes[0]['metadata']['name'] != journal['volume'] or
            volumes[0]['spec'].get('dataEngine') != 'v2'):
        raise ValueError('interrupt mode refuses missing, replaced or foreign volumes')
    return volumes[0]


def detached(lab, journal):
    volume = only_business(lab, journal)
    if volume.get('status', {}).get('state') != 'detached' or volume['spec'].get('nodeID'):
        return False
    for kind in ('engines.longhorn.io', 'enginefrontends.longhorn.io', 'replicas.longhorn.io'):
        for obj in lab.get(kind, namespace=NS)['items']:
            if obj['spec'].get('volumeName') != journal['volume']:
                raise ValueError('interrupt mode refuses foreign native consumers')
            if obj.get('status', {}).get('currentState') not in ('stopped', ''):
                return False
    return True


def product(lab, pid, enabled, *, refusal=False):
    """Use the existing private input journal and supervised kubecli entry."""
    path = lab.DIRECTORY/'config.yml'
    receipt = lab.DIRECTORY/'longhorn-request-input.json'
    if path.is_symlink() or receipt.exists() or receipt.is_symlink():
        raise ValueError('interrupt mode input has an unresolved operation')
    original = path.read_text()
    row, journal = load_journal(lab)
    if original != row['original_config']:
        raise ValueError('interrupt mode product input differs from the original declaration')
    native_identity(lab, row['original'])
    only_business(lab, journal)
    if not refusal and not detached(lab, journal):
        raise ValueError('interrupt-mode change requires all owned native instances detached')
    config = yaml.safe_load(original)
    if config['longhorn_requests'] or config['longhorn_data_engines'] != ['v2']:
        raise ValueError('interrupt mode requires the owned V2-only idle product declaration')
    config['longhorn_v2_interrupt_mode'] = enabled
    desired = yaml.safe_dump(config, sort_keys=False)
    lab.write_private(receipt, {'run_id': lab.owner()['run_id'], 'original': original, 'desired': desired})
    with lab.scoped_cleanup(lab.restore_request_input):
        with tempfile.NamedTemporaryFile('w', dir=lab.DIRECTORY, delete=False) as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.chmod(0o600)
        temporary.replace(path)
        lab.supervise_core(pid, 'longhorn-setup', expected_refusal=REFUSAL if refusal else None)


def load_journal(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('symlink interrupt-mode journal')
    row = json.loads(path.read_text())
    state = lab.owner()
    if (any(row.get(key) != state[key] for key in ('run_id', 'cluster_uid')) or
            row.get('installation_namespace_uid') != state['namespace_uid']):
        raise ValueError('interrupt-mode journal belongs to another installation')
    original = yaml.safe_load(row['original_config'])
    if (original['longhorn_data_engines'] != ['v2'] or original['longhorn_v2_interrupt_mode'] is not False or
            json.loads(row['original']['setting_value']).get('v2') != 'false'):
        raise ValueError('interrupt-mode journal is outside the polling baseline')
    business_path = lab.DIRECTORY/BUSINESS
    if business_path.is_symlink():
        raise ValueError('symlink interrupt-mode business journal')
    journal = json.loads(business_path.read_text())
    if any(journal.get(k) != row[k] for k in ('namespace', 'namespace_uid', 'volume_uid')):
        raise ValueError('interrupt-mode business identity changed')
    return row, journal


def stop_clients(lab, journal):
    only_business(lab, journal)
    for key, uid in journal['objects'].items():
        if not key.startswith('Pod/'):
            continue
        name = key.split('/', 1)[1]
        pod = lab.get('pod', name, journal['namespace'])
        if pod and pod['metadata']['uid'] != uid:
            raise ValueError('interrupt-mode client was replaced')
    for key, uid in journal['objects'].items():
        if key.startswith('Pod/'):
            lab.load_helper('longhorn-v2-clone-lifecycle.py').delete(
                lab, 'pod', key.split('/', 1)[1], uid, journal['namespace'])
    lab.wait('interrupt-mode-all-instances-detached', 300, lambda: detached(lab, journal))


def restore(lab, pid):
    path = lab.DIRECTORY/RECORD
    if not path.exists() and not path.is_symlink():
        return
    if pid <= 0:
        raise ValueError('interrupt-mode profile recovery requires the runner-owned live relay')
    row, journal = load_journal(lab)
    # restore_request_input validates the shared receipt's run and exact bytes.
    receipt = lab.DIRECTORY/'longhorn-request-input.json'
    if receipt.exists() or receipt.is_symlink():
        if receipt.is_symlink():
            raise ValueError('symlink interrupt-mode product input journal')
        data = json.loads(receipt.read_text())
        if data.get('original') != row['original_config']:
            raise ValueError('interrupt-mode recovery refuses a foreign input journal')
        lab.restore_request_input()
    if (lab.DIRECTORY/'config.yml').is_symlink() or (lab.DIRECTORY/'config.yml').read_text() != row['original_config']:
        raise ValueError('interrupt-mode configuration changed outside its journal')
    native_identity(lab, row['original'])
    stop_clients(lab, journal)
    product(lab, pid, False)
    restored = lab.wait('interrupt-mode-original-polling-ready', 1200,
                        lambda: components(lab, False, row['original']))
    if restored['setting_value'] != row['original']['setting_value']:
        raise ValueError('interrupt-mode original Setting value was not restored exactly')
    lab.installation_health()
    path.unlink()
    print('LONGHORN_V2_INTERRUPT_ORIGINAL_PROFILE_RESTORED', flush=True)


def cleanup(lab, pid):
    restore(lab, pid)
    lab.cleanup_business(relay_pid=pid)


def business(lab, pid):
    record = lab.DIRECTORY/BUSINESS
    if any((lab.DIRECTORY/n).exists() or (lab.DIRECTORY/n).is_symlink() for n in (BUSINESS, RECORD)):
        raise ValueError('interrupt-mode qualification requires previous scoped cleanup')
    lab.run_requests(pid, [])
    original = lab.wait('interrupt-mode-polling-baseline', 120, lambda: components(lab, False))
    config = (lab.DIRECTORY/'config.yml').read_text()
    if yaml.safe_load(config)['longhorn_v2_interrupt_mode'] is not False:
        raise ValueError('interrupt-mode qualification requires an explicit false baseline')
    namespace = 'longhorn-business-'+secrets.token_hex(8)
    ns = json.loads(lab.kubectl('create', 'namespace', namespace, '-o', 'json').stdout)
    journal = {'namespace': namespace, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
               'storage_class': 'longhorn', 'reclaim_policy': 'Retain'}
    lab.write_private(record, journal)
    create = lambda obj: lab.create_business_object(journal, record, obj)
    lifecycle = lab.load_helper('longhorn-v2-clone-lifecycle.py')
    token = lab.owner()['run_id']
    def client(name, index):
        create(lab.client_pod(namespace, name, 'data', 'longhorn-node-'+lab.WORKERS[index].rsplit('.', 1)[1]))
        def current():
            pod = lifecycle.current(lab, 'pod', name, journal['objects']['Pod/'+name], namespace)
            return ready(pod)
        lab.wait('interrupt-mode-client-'+name, 600, current)
    with lab.scoped_cleanup(lambda: cleanup(lab, pid)):
        journal['defaults'] = lab.wait('interrupt-mode-namespace-defaults', 60, lambda: lab.namespace_defaults(namespace))
        lab.write_private(record, journal)
        claim = lab.clone_claim(namespace, 'data', '2Gi')
        del claim['spec']['dataSource']
        create(claim)
        def bound():
            lab.bind_business_volume(journal, record)
            return journal.get('volume_uid')
        lab.wait('interrupt-mode-source-bound', 600, bound)
        client('writer', 0)
        digest = lifecycle.io(lab, journal, 'writer', token)
        lab.wait('interrupt-mode-source-healthy', 600, lambda: lab.healthy_replicas(journal['volume']))
        topology = lab.replica_topology(journal, 3)
        row = {key: lab.owner()[key] for key in ('run_id', 'cluster_uid')}
        row.update(namespace=namespace, namespace_uid=journal['namespace_uid'], volume_uid=journal['volume_uid'],
                   original=original, original_config=config)
        # Installation and business namespaces are distinct identities.
        row['installation_namespace_uid'] = lab.owner()['namespace_uid']
        lab.write_private(lab.DIRECTORY/RECORD, row)
        product(lab, pid, True, refusal=True)
        if components(lab, False, original) != original:
            raise ValueError('attached interrupt-mode refusal changed native components')
        lifecycle.io(lab, journal, 'writer', token, read=True)
        print('LONGHORN_V2_INTERRUPT_ATTACHED_REFUSED', flush=True)
        stop_clients(lab, journal)
        product(lab, pid, True)
        active = lab.wait('interrupt-mode-enabled-ready', 1200, lambda: components(lab, True, original))
        if any(active['managers'][n]['pod_uid'] == p['pod_uid'] for n, p in original['managers'].items()):
            raise ValueError('interrupt-mode setting did not recreate every native Pod')
        client('interrupt-reader', 1)
        lifecycle.io(lab, journal, 'interrupt-reader', token, read=True)
        faults = lab.load_helper('longhorn-faults.py')
        control = lab.load_helper('longhorn-control-faults.py')
        ledger_token = token+':interrupt'
        program = faults.ledger_program(ledger_token, duration=5).replace('stop=ledger_stop_requested', 'stop=None')
        output = lab.kubectl('-n', namespace, 'exec', 'interrupt-reader', '--', 'python3', '-u', '-c', program, timeout=30)
        ledger = control.confirmed([json.loads(s) for s in output.stdout.splitlines()], ledger_token, faults)
        faults.verify_client_ledger(lab, journal, 'interrupt-reader', ledger)
        stop_clients(lab, journal)
        product(lab, pid, False)
        restored = lab.wait('interrupt-mode-restored-ready', 1200, lambda: components(lab, False, original))
        if any(restored['managers'][n]['pod_uid'] == p['pod_uid'] for n, p in active['managers'].items()):
            raise ValueError('polling restoration did not recreate every native Pod')
        client('polling-reader', 2)
        lifecycle.io(lab, journal, 'polling-reader', token, read=True)
        faults.verify_client_ledger(lab, journal, 'polling-reader', ledger)
        lab.wait('interrupt-mode-original-replicas-healthy', 600, lambda: lab.healthy_replicas(journal['volume']))
        if lab.replica_topology(journal, 3) != topology:
            raise ValueError('interrupt-mode roundtrip changed original replica identities')
        print('LONGHORN_V2_INTERRUPT_RESULT '+json.dumps({'volume_uid': journal['volume_uid'],
            'payload_bytes': len(token.encode())*262144, 'payload_sha256': digest,
            'original': original, 'interrupt': active, 'restored': restored, 'replicas': topology,
            'interrupt_readback': True, 'polling_readback': True, 'ledger_cross_node_readback': True,
            'ledger': ledger}, sort_keys=True), flush=True)
    print('LONGHORN_V2_INTERRUPT_BUSINESS_PASS', flush=True)


def verify_log(log):
    rows = [json.loads(s.split(' ', 1)[1]) for s in log.splitlines()
            if s.startswith('LONGHORN_V2_INTERRUPT_RESULT ')]
    if len(rows) != 1:
        raise ValueError('interrupt-mode result is missing or duplicated')
    row = rows[0]
    original, active, restored = (row[k] for k in ('original', 'interrupt', 'restored'))
    token = row['ledger']['token'].removesuffix(':interrupt')
    if (not token or row['ledger']['token'] != token+':interrupt' or not row.get('volume_uid') or
            row['payload_bytes'] != len(token.encode())*262144 or
            row['payload_sha256'] != hashlib.sha256(token.encode()*262144).hexdigest() or
            len(row['replicas']) != 3 or len({r['node'] for r in row['replicas'].values()}) != 3 or
            len({r['uid'] for r in row['replicas'].values() if r['uid']}) != 3 or
            len(original['managers']) != 3 or original['setting_value'] != restored['setting_value'] or
            not all(row.get(k) is True for k in ('interrupt_readback', 'polling_readback', 'ledger_cross_node_readback'))):
        raise ValueError('interrupt-mode result lacks the original data, replicas or restored setting')
    for state, enabled in ((original, False), (active, True), (restored, False)):
        if (not state['setting_uid'] or state['setting_uid'] != original['setting_uid'] or
                json.loads(state['setting_value']).get('v2') != str(enabled).lower() or
                set(state['managers']) != set(original['managers'])):
            raise ValueError('interrupt-mode native settings or manager set differ')
        for name, im in state['managers'].items():
            if (not im.get('uid') or any(im[k] != original['managers'][name][k] for k in ('uid', 'spec')) or
                    ('--spdk-interrupt-mode' in im['args']) != enabled or not im.get('pod_uid')):
                raise ValueError('interrupt-mode native manager identity or flag differs')
    for name, im in original['managers'].items():
        if (len({s['managers'][name]['pod_uid'] for s in (original, active, restored)}) != 3 or
                im['args'] != restored['managers'][name]['args']):
            raise ValueError('interrupt-mode result lacks actual Pod roundtrip and original flags')
    control = load('longhorn-control-faults.py')
    faults = load('longhorn-faults.py')
    if control.confirmed(row['ledger']['records'], row['ledger']['token'], faults) != row['ledger']:
        raise ValueError('interrupt-mode acknowledgement ledger differs')
    for marker in ('LONGHORN_V2_INTERRUPT_ATTACHED_REFUSED', 'LONGHORN_V2_INTERRUPT_ORIGINAL_PROFILE_RESTORED',
                   'LONGHORN_V2_INTERRUPT_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('interrupt-mode log lacks a unique refusal, recovery, business or cleanup result')
    if re.search(r'^LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)', log, re.M):
        raise ValueError('interrupt-mode log contains a failure')
    return row


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    if args.mode == 'business' and args.relay_pid <= 0:
        parser.error('interrupt-mode changes require the runner-owned live relay')
    (business if args.mode == 'business' else cleanup)(lab, args.relay_pid)


if __name__ == '__main__':
    main()
