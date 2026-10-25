#!/usr/bin/env python3
"""Independent V2 fixture identity using the qualified Longhorn transport.

The V1 module is loaded in a separate namespace. Its original constants, files,
transport qualification and live installation remain unchanged.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import sys
import uuid

import yaml


BASE = Path('/usr/local/kubeauto')
CLUSTER = 'longhorn-v2-gate'
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
DIRECTORY = BASE/'clusters'/CLUSTER


def inventory():
    lines = []
    for group in ('etcd', 'kube_master', 'kube_node'):
        lines.append('[' + group + ']')
        lines.extend(ip + " k8s_nodename='longhorn-node-" + ip.rsplit('.', 1)[1] + "'" for ip in NODES)
    lines.extend(('[all:vars]', 'SECURE_PORT="6443"', 'CONTAINER_RUNTIME="containerd"',
                  'CLUSTER_NETWORK="calico"', 'PROXY_MODE="ipvs"', 'SERVICE_CIDR="10.87.0.0/16"',
                  'CLUSTER_CIDR="172.31.0.0/16"', 'NODE_PORT_RANGE="30000-32767"',
                  'CLUSTER_DNS_DOMAIN="cluster.local"', 'bin_dir="/usr/local/bin"',
                  'base_dir="/usr/local/kubeauto"',
                  'cluster_dir="{{ base_dir }}/clusters/longhorn-v2-gate"',
                  'ca_dir="/etc/kubernetes/ssl"', "k8s_nodename=''", 'ansible_user=root'))
    return '\n'.join(lines) + '\n'


def fixture():
    spec = importlib.util.spec_from_file_location('longhorn_v2_fixture', BASE/'tests/helpers/longhorn-lab.py')
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    lab.BASE = BASE
    lab.CLUSTER = CLUSTER
    lab.NODES = lab.WORKERS = NODES
    lab.DIRECTORY = DIRECTORY
    lab.STATE = DIRECTORY/'longhorn-lab-owner.json'
    # The qualified listener identity contract pins 23520. It is host-local;
    # V2 uses disjoint nodes and control sockets under its own directory.
    lab.inventory = inventory
    def ensure(force=False):
        relay_ensure(lab, force=force)
    lab.relay_ensure = ensure
    inherited_fingerprint = lab.transport_fingerprint
    def fingerprint():
        return hashlib.sha256(inherited_fingerprint().encode() + Path(__file__).read_bytes()).hexdigest()
    lab.transport_fingerprint = fingerprint
    return lab


def relay_probe(lab, node):
    # Python is already required by Ansible. Minimal Ubuntu installations may
    # have no curl; avoid installing a host package solely for this fixture.
    script = '''import urllib.request
request=urllib.request.Request('http://192.168.47.130:5000/v2/')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request,timeout=5) as response:
 assert response.status==200
'''
    return lab.remote(node, ['python3', '-c', script], check=False).returncode == 0


def relay_ensure(lab, *, force=False):
    """Use existing owner/listener/cleanup primitives with a three-node probe."""
    state = lab.owner()
    rows = lab.relay_rows(state)
    for node in NODES:
        row = next((row for row in rows if row['node'] == node), None)
        if row:
            live = lab.execute(lab.SSH + ['-S', row['socket'], '-O', 'check', 'root@' + node], check=False)
            if live.returncode:
                lab.remote(node, lab.relay_rule('-C'))
                lab.remote(node, script='test -z "$(ss -H -lnt ' + shlex.quote('sport = :' + str(lab.PORT)) + ')"')
                socket = Path(row['socket'])
                if socket.exists():
                    socket.unlink()
                lab.relay_connect(row)
                lab.bind_listener(row, state)
        elif force or not relay_probe(lab, node):
            row = {'node': node, 'socket': str(DIRECTORY/('longhorn-registry-' + node + '.sock'))}
            if Path(row['socket']).exists() or lab.remote(node, lab.relay_rule('-C'), check=False).returncode != 1:
                raise ValueError('foreign V2 relay socket/rule')
            lab.remote(node, script='test -z "$(ss -H -lnt ' + shlex.quote('sport = :' + str(lab.PORT)) + ')"')
            rows.append(row)
            state['relays'] = rows
            lab.write_private(lab.STATE, state)
            lab.relay_connect(row)
            lab.bind_listener(row, state)
            lab.remote(node, lab.relay_rule('-A'))
        if not relay_probe(lab, node):
            raise ValueError('V2 Registry transport did not return HTTP success node=' + node)
    print('LONGHORN_V2_REGISTRY_TRANSPORT_READY nodes=3 endpoint=unchanged', flush=True)


def configure(lab):
    if lab.STATE.exists() or DIRECTORY.is_symlink():
        raise ValueError('new V2 fixture requires no prior owner')
    config_path = DIRECTORY/'config.yml'
    hosts = DIRECTORY/'hosts'
    if config_path.is_symlink() or hosts.is_symlink():
        raise ValueError('symlink V2 input')
    config = yaml.safe_load(config_path.read_text())
    if config['longhorn_version'] != '1.12.1':
        raise ValueError('V2 fixture requires the pinned product defaults')
    # Registry qualification does not declare a disk or enable Longhorn. Later
    # product gates must bind the live disk identity and resource preparation.
    config.update(K8S_VER='1.33.6', CLUSTER_NAME=CLUSTER, ENABLE_SETTING_HOSTNAME=False,
                  KUBE_RESERVED_ENABLED='no', SYS_RESERVED_ENABLED='no', longhorn_install='no',
                  longhorn_storage_nodes=[], longhorn_v2_nodes=[], longhorn_block_disks={})
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    hosts.write_text(inventory())
    lab.write_private(lab.STATE, {'cluster': CLUSTER, 'nodes': list(NODES), 'run_id': str(uuid.uuid4()),
                                 'clean_start_verified': True, 'disks': {}, 'healthy': False})
    print('LONGHORN_V2_LAB_CONFIGURED scope=registry-only')


def host_inputs():
    spec = importlib.util.spec_from_file_location('longhorn_v2_inputs', BASE/'tests/helpers/longhorn-v2-pool.py')
    pool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pool)
    for host in NODES:
        row = pool.collect(host)
        if row['assessment']['blockers']:
            raise ValueError('V2 registry qualification requires clean hosts: ' + host + ' ' +
                             ','.join(row['assessment']['blockers']))
        print('LONGHORN_V2_REGISTRY_HOST_INPUT ' + json.dumps(row, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('host-inputs', 'configure', 'relay-qualification',
                                       'relay-ensure', 'relay-clean', 'qualification-clean'))
    args = parser.parse_args()
    lab = fixture()
    lab.require_lease()
    if args.mode == 'configure':
        configure(lab)
    elif args.mode == 'host-inputs':
        host_inputs()
    else:
        {'relay-qualification': lab.relay_qualification, 'relay-ensure': lab.relay_ensure,
         'relay-clean': lab.relay_cleanup, 'qualification-clean': lab.qualification_clean}[args.mode]()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        print('LONGHORN_V2_FIXTURE_FAIL reason=' + str(error), file=sys.stderr)
        sys.exit(1)
