"""Bind effective product inventory to the existing read-only source observer.

Ansible resolves groups, host precedence and K8S_NODENAME with the same config
as production. Only allowlisted debug fields are consumed; inventory credentials
and arbitrary task output are never copied into the receipt or an exception.
This internal observation does not authorize mutation or sign UPG-05.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace

from upgrade.product.kubernetes_preflight import NODE_NAME, Refused, endpoint, observe


BOOK = Path(__file__).with_suffix('.yml')
CONTRACT = 'kubeauto-upgrade-inventory-v1'
GROUPS = ('etcd', 'kube_master', 'kube_node')


def inventory_event(event):
    """Retain only allowlisted observation fields before runner temp cleanup."""
    if event.get('event') != 'runner_on_ok':
        return None
    data = event.get('event_data', {})
    message = data.get('res', {}).get('msg')
    if not isinstance(message, dict) or message.get('contract') != CONTRACT:
        return None
    keys = ('contract', 'kind', 'groups') if message.get('kind') == 'topology' else (
        'contract', 'kind', 'host', 'transport_host', 'node_name', 'api_port', 'runtime', 'network')
    return {'event': 'runner_on_ok', 'event_data': {
        'host': data.get('host'), 'task': data.get('task'),
        'res': {'msg': {key: deepcopy(message[key]) for key in keys if key in message}}}}


def collect(result):
    """Refuse failed, limited, duplicated or incomplete runner observations."""
    if type(result.rc) is not int or result.rc != 0:
        raise Refused('effective inventory observation failed; retain private runner diagnostics')
    topology = None
    hosts = {}
    events = getattr(result, 'upgrade_inventory_events', None)
    if events is None:
        events = result.events
    for event in events:
        if event.get('event') != 'runner_on_ok':
            continue
        data = event.get('event_data', {})
        message = data.get('res', {}).get('msg')
        if not isinstance(message, dict) or message.get('contract') != CONTRACT:
            continue
        if message.get('kind') == 'topology':
            if (topology is not None or data.get('host') != 'localhost' or
                    data.get('task') != 'Observe upgrade inventory topology'):
                raise Refused('inventory topology observation is duplicated or foreign')
            topology = message.get('groups')
        elif message.get('kind') == 'host':
            host = message.get('host')
            if (not isinstance(host, str) or not host or host in hosts or
                    data.get('host') != host or data.get('task') != 'Observe upgrade inventory host'):
                raise Refused('inventory host observation is duplicated or foreign')
            hosts[host] = deepcopy(message)
        else:
            raise Refused('unknown inventory observation contract')
    if not isinstance(topology, dict) or set(topology) != set(GROUPS):
        raise Refused('complete inventory topology was not observed')
    for group in GROUPS:
        members = topology[group]
        if (not isinstance(members, list) or
                not all(isinstance(host, str) and host and not any(c.isspace() for c in host)
                        for host in members) or len(set(members)) != len(members)):
            raise Refused('inventory group members are invalid or duplicated')
    if not topology['kube_master'] or len(topology['etcd']) not in (1, 3, 5):
        raise Refused('complete master and odd etcd topology are required')
    expected = set().union(*(set(topology[g]) for g in GROUPS))
    if set(hosts) != expected:
        raise Refused('inventory observation is limited or omits a required host')
    # Existing product units and the etcd/control-plane stages use the inventory
    # hostname as their service address. Aliases need a separately reviewed path.
    if any(value.get('transport_host') != host for host, value in hosts.items()):
        raise Refused('inventory transport aliases require review against actual service addresses')
    nodes = {}
    for host in dict.fromkeys(topology['kube_master'] + topology['kube_node']):
        value = hosts[host]
        name = value.get('node_name')
        if not isinstance(name, str) or not NODE_NAME.fullmatch(name) or name in nodes.values():
            raise Refused('effective node names must be valid and unique')
        if value.get('runtime') not in ('containerd', 'docker') or value.get('network') not in (
                'calico', 'flannel', 'cilium', 'kube-router', 'kube-ovn'):
            raise Refused('effective runtime or network is outside the product paths')
        nodes[host] = name
    if (len({hosts[h]['runtime'] for h in nodes}) != 1 or
            len({hosts[h]['network'] for h in nodes}) != 1):
        raise Refused('mixed effective runtime or network requires an explicit reviewed plan')
    backends = []
    for host in topology['kube_master']:
        port = hosts[host].get('api_port')
        if (isinstance(port, bool) or not isinstance(port, (str, int)) or
                not str(port).isdigit() or not 1 <= int(port) <= 65535):
            raise Refused('effective API secure port is invalid')
        address = '[' + host + ']' if ':' in host else host
        backends.append(endpoint(f'https://{address}:{int(port)}'))
    return {'groups': deepcopy(topology), 'hosts': hosts, 'nodes': nodes, 'api_backends': backends,
            'runtime': hosts[next(iter(nodes))]['runtime'],
            'network': hosts[next(iter(nodes))]['network']}


def observe_source(run_playbook, variables, *, kubectl, kubectl_sha256, kubeconfig, output):
    """Use the actual product runner callback and then the real API observer.

    The caller passes the full config as extra-vars, as ClusterManager does.
    Output remains owned by the observer's caller; no file is written here.
    """
    if (not callable(run_playbook) or not isinstance(variables, dict) or
            variables.get('K8S_VER') not in ('1.34.13', 'v1.34.13')):
        raise Refused('exact first-hop target config and actual product runner are required')
    before = hashlib.sha256(BOOK.read_bytes()).hexdigest()
    inventory = collect(run_playbook(BOOK, extra_vars=deepcopy(variables), controller_only=True))
    if hashlib.sha256(BOOK.read_bytes()).hexdigest() != before:
        raise Refused('inventory observation implementation changed while reading')
    # The mature kube-node role intentionally cordons masters. Bind that
    # product-owned baseline to actual group membership; an unexpected worker
    # cordon or master uncordon still refuses before mutation.
    expected_schedulable = None
    if variables.get('UPGRADE_EXPECT_MASTER_UNSCHEDULABLE') is True:
        expected_schedulable = {
            name: host in inventory['groups']['kube_node']
            for host, name in inventory['nodes'].items()
        }
    receipt = observe(SimpleNamespace(source='v1.33.6', target='v1.34.13',
        api_server=inventory['api_backends'], node=list(inventory['nodes'].values()),
        **({} if expected_schedulable is None else
           {'originally_schedulable': expected_schedulable}),
        kubectl=Path(kubectl), kubectl_sha256=kubectl_sha256,
        kubeconfig=Path(kubeconfig), output=Path(output)))
    receipt['effective_inventory'] = inventory
    receipt['inventory_observer_sha256'] = before
    return receipt
