#!/usr/bin/env python3
"""Runner-owned independent V2 core; all cluster changes use config + kubecli."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import yaml

BASE = Path('/usr/local/kubeauto')


def load(filename):
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), BASE/'tests/helpers'/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    profile = load('longhorn-v2-lab.py')
    lab = profile.fixture()
    def identity():
        core_identity(lab)
    lab.core_identity = identity
    return profile, lab


def verify_transport(lab):
    evidence = json.loads((BASE/'tests/evidence/longhorn-v2-registry-qualification-20261020.json').read_text())
    if (evidence['status'] != 'pass' or evidence['rc'] or evidence['failure_markers'] or
            evidence['transport_fingerprint'] != lab.transport_fingerprint()):
        raise ValueError('V2 transport requires current focused qualification')
    print('LONGHORN_V2_TRANSPORT_QUALIFICATION_CURRENT')


def configure(profile, lab):
    # Read all hosts before creating an owner or modifying the generated config.
    pool = load('longhorn-v2-pool.py')
    raw = {}
    for address in profile.NODES:
        row = pool.collect(address)
        if row['assessment']['blockers']:
            raise ValueError('V2 core host inputs are not clean: ' + address)
        host = row['inventory']
        spans = host['online_cpus'].split(',')
        if not all(re.fullmatch(r'\d+(?:-\d+)?', span) for span in spans):
            raise ValueError('invalid V2 fixture CPU inventory')
        cpus = set()
        for span in spans:
            ends = list(map(int, span.split('-')))
            if ends[0] > ends[-1]:
                raise ValueError('invalid V2 fixture CPU range')
            cpus.update(range(ends[0], ends[-1] + 1))
        # Planned normal profile: one reactor, 25 percent guaranteed CPU and
        # 2Gi hugepages. Actual kubelet allocatable admission follows setup.
        if 0 not in cpus or len(cpus) < 4 or host['memory_kib']['MemTotal'] < 5 * 1024**2:
            raise ValueError('V2 fixture lacks resources for its planned CPU/hugepage profile')
        disk = row['assessment']['candidates'][0]
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', host['hostname']):
            raise ValueError('V2 fixture requires a restorable host name')
        paths = sorted(disk['by_id'], key=lambda path: (not Path(path).name.startswith('wwn-'), path))
        raw[address] = {'path': paths[0], 'identity': disk['identity'], 'size_bytes': disk['size_bytes'],
                        'machine_id': host['machine_id'], 'original_hostname': host['hostname']}
    profile.configure(lab)
    path = lab.DIRECTORY/'config.yml'
    config = yaml.safe_load(path.read_text())
    # Calico's etcd datastore uses the system hostname unless NODENAME is set.
    # Cloned VMs may share it even though kubelet has distinct overrides. Use
    # the product's standard hostname step, never an out-of-band live repair.
    config['ENABLE_SETTING_HOSTNAME'] = True
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    state = lab.owner()
    state['raw_disks'] = raw
    lab.write_private(lab.STATE, state)
    print('LONGHORN_V2_CORE_INPUTS_PINNED nodes=3 raw_disks=3 data_untouched=true')


def core_identity(lab):
    state = lab.owner()
    system = lab.get('namespace', 'kube-system')['metadata']['uid']
    nodes = lab.get('nodes')['items']
    expected = {'longhorn-node-' + ip.rsplit('.', 1)[1]: ip for ip in lab.NODES}
    if {node['metadata']['name'] for node in nodes} != set(expected):
        raise ValueError('foreign or missing V2 core node')
    for node in nodes:
        ip = expected[node['metadata']['name']]
        if [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP'] != [ip]:
            raise ValueError('V2 core address identity changed')
        if not any(condition['type'] == 'Ready' and condition['status'] == 'True'
                   for condition in node['status']['conditions']):
            raise ValueError('V2 core node is not Ready')
    uids = {node['metadata']['name']: node['metadata']['uid'] for node in nodes}
    if state.get('cluster_uid') and (state['cluster_uid'] != system or state['node_uids'] != uids):
        raise ValueError('runner-owned V2 core identity changed')
    if not state.get('cluster_uid'):
        state.update(cluster_uid=system, node_uids=uids)
        lab.write_private(lab.STATE, state)
    print('LONGHORN_V2_CORE_IDENTITY_PASS nodes=3')


def supervise(lab, relay_pid, step):
    lab.owner()
    identity = lab.process_identity(relay_pid)
    expected = [str(Path(__file__).resolve()).encode(), b'relay-maintain']
    if not identity or identity[0][-2:] != expected:
        raise ValueError('V2 live Registry supervisor is a product prerequisite')
    commands = {'download': ['download', '-D'], 'setup': ['setup', lab.CLUSTER, '90']}
    argv = [sys.executable, str(BASE/'kubecli.py'), *commands[step]]
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
    started = time.monotonic()
    heartbeat = started
    with lab.scoped_cleanup(lambda: lab.stop_product(process, argv)):
        while process.poll() is None:
            if lab.process_identity(relay_pid) != identity:
                raise ValueError('V2 Registry supervisor exited; stopping dependent product step')
            now = time.monotonic()
            if now - started >= 7200:
                raise TimeoutError('V2 product step exceeded two hours')
            if now - heartbeat >= 15:
                print('LONGHORN_V2_PRODUCT_HEARTBEAT step=' + step +
                      ' elapsed=' + str(int(now - started)), flush=True)
                heartbeat = now
            time.sleep(1)
        if process.returncode:
            raise ValueError('V2 product step failed: ' + step + ' rc=' + str(process.returncode))


def cleanup(lab):
    state = lab.owner()
    if state.get('namespace_uid') or state['disks'] or state.get('relays'):
        raise ValueError('V2 core cleanup cannot bypass storage or transport recovery')
    if set(state.get('raw_disks', {})) != set(lab.NODES):
        raise ValueError('V2 core recovery requires all original host identities')
    resource_journal = lab.DIRECTORY/'longhorn-v2-resources.json'
    if resource_journal.exists() or resource_journal.is_symlink():
        raise ValueError('restore V2 resource fixtures before removing their core')
    for node in lab.NODES:
        current = lab.remote(node, ['cat', '/etc/machine-id']).stdout.strip()
        if current != state['raw_disks'][node]['machine_id']:
            raise ValueError('V2 core recovery host identity changed')
        hostname = lab.remote(node, ['hostname']).stdout.strip()
        if hostname not in (state['raw_disks'][node]['original_hostname'], 'longhorn-node-' + node.rsplit('.', 1)[1]):
            raise ValueError('V2 core recovery host name changed outside the product')
    command = ['bash', BASE/'tests/helpers/lab-wipe-nodes.sh']
    lab.execute(command + ['--rocky-only', *lab.NODES], timeout=900)
    verified = lab.execute(command + ['--verify', '--rocky-only', *lab.NODES], timeout=900)
    print(verified.stdout, end='', flush=True)
    for node in lab.NODES:
        row = state['raw_disks'][node]
        script = '''import pathlib,socket,subprocess,sys
assert pathlib.Path('/etc/machine-id').read_text().strip()==sys.argv[1], 'changed V2 host identity'
current=socket.gethostname()
assert current in (sys.argv[2],sys.argv[3]), 'changed V2 host name'
if current!=sys.argv[3]: subprocess.run(['hostnamectl','set-hostname',sys.argv[3]],check=True,timeout=30)
assert socket.gethostname()==sys.argv[3], 'V2 original hostname not restored'
'''
        lab.remote(node, ['python3', '-c', script, row['machine_id'],
                          'longhorn-node-' + node.rsplit('.', 1)[1], row['original_hostname']])
    lab.archive_core()
    print('LONGHORN_V2_CORE_RECOVERY_SCOPED_CLEAN_PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('verify-transport', 'configure', 'verify-core', 'relay-maintain',
                                        'relay-ensure', 'relay-clean', 'supervise', 'smoke', 'cleanup'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--step', choices=('download', 'setup'))
    args = parser.parse_args()
    profile, lab = fixture()
    lab.require_lease()
    if args.mode == 'verify-transport':
        verify_transport(lab)
    elif args.mode == 'configure':
        configure(profile, lab)
    elif args.mode == 'supervise':
        if args.step is None:
            parser.error('--step is required for supervise')
        supervise(lab, args.relay_pid, args.step)
    else:
        {'verify-core': lab.core_identity, 'relay-maintain': lab.relay_maintain,
         'relay-ensure': lab.relay_ensure, 'relay-clean': lab.relay_cleanup,
         'smoke': lab.core_smoke, 'cleanup': lambda: cleanup(lab)}[args.mode]()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, TimeoutError) as error:
        print('LONGHORN_V2_CORE_FAIL reason=' + str(error), file=sys.stderr)
        sys.exit(1)
