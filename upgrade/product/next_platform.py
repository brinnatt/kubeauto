"""Second-hop platform prerequisites, using the existing guarded component stages."""
from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile

from upgrade.product.calico_stage import CalicoStage, SECRET, content, matches, manifests, healthy
from upgrade.product.kubernetes_preflight import Refused, mapping, query
from upgrade.product.hop import PINS_PATH

PRODUCT = Path(__file__).parent


def snapshot(path, collect):
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o777 != 0o600:
            raise Refused('platform snapshot must be private and owned')
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            raise Refused('platform snapshot is incomplete; retain it for diagnosis') from None
    value = collect()
    # Publish a complete file exclusively. A kill before publication cannot
    # leave a truncated accepted boundary; concurrent owners cannot overwrite it.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
    return value


class PlatformPrerequisites:
    def __init__(self, manager, cluster, source, variables, state, traffic):
        self.manager, self.cluster, self.source = manager, cluster, source
        self.variables, self.state, self.traffic = dict(variables), state, traffic
        inventory = source['effective_inventory']
        if inventory['network'] != 'calico':
            raise Refused('next-hop live path currently requires the reviewed Calico source profile')
        parameters = [inventory['hosts'][h].get('parameters') for h in inventory['nodes']]
        if (not parameters or not isinstance(parameters[0], dict) or
                any(p != parameters[0] for p in parameters) or
                any(not isinstance(v, str) or not v for v in parameters[0].values())):
            raise Refused('complete consistent effective platform parameters required')
        self.variables.update(parameters[0])
        self.variables.update(groups=inventory['groups'],
            ETCD_ENDPOINTS=','.join('https://'+h+':2379' for h in inventory['groups']['etcd']))
        # inventory.yml resolves these with the same external Ansible process
        # and precedence as installation. Do not embed ansible-core in kubecli
        # or reimplement its recursive variable semantics here.
        for key in ['IP_AUTODETECTION_METHOD', 'CALICO_NETWORKING_BACKEND', 'CALICO_ENABLE_OVERLAY']:
            if key not in parameters[0] or '{{' in parameters[0][key]:
                raise Refused('effective Calico parameters were not resolved by product inventory')
        self.binary = manager.kube_bin_dir/'kubectl'
        self.kubeconfig = manager.clusters_dir/cluster/'kubectl.kubeconfig'
        self.etcdctl = Path(self.variables['base_dir'])/'extra-bin/etcdctl'
        ssl = manager.clusters_dir/cluster/'ssl'
        self.etcd_args = [str(self.etcdctl), '--endpoints='+self.variables['ETCD_ENDPOINTS'],
            '--cacert='+str(ssl/'ca.pem'), '--cert='+str(ssl/'etcd.pem'),
            '--key='+str(ssl/'etcd-key.pem'), '--dial-timeout=5s', '--command-timeout=15s']
        self.original_datastore = snapshot(state/'calico-datastore.json', self.datastore)
        self.retained = (source.get('source'), source.get('target')) == ('v1.35.10', 'v1.36.6')
        if self.retained:
            self.retained_objects = snapshot(state/'calico-retained-objects.json', self.read_retained)
        self.edges = snapshot(state/'calico-edges.json', self.planned_edges)
        if self.edges not in [[], [['v3.31.7', 'v3.32.4']],
                             [['v3.28.4', 'v3.31.7'], ['v3.31.7', 'v3.32.4']]]:
            raise Refused('durable Calico edge plan is outside the admitted source profiles')
        if self.retained and self.edges:
            raise Refused('this hop retains Calico and cannot replay historical migration edges')

    def read_retained(self):
        objects = {}
        expected = manifests(self.variables, 'v3.32.4')
        for ref in ['daemonset/calico-node', 'deployment/calico-kube-controllers', SECRET]:
            obj = mapping(query(self.binary, self.kubeconfig, self.source['api_backends'][0]['server'],
                ['-n', 'kube-system', 'get', ref, '-o', 'json']))
            if ref in expected and not matches(obj, expected[ref]):
                raise Refused('retained Calico differs from its reviewed 3.32.4 declaration')
            if ref != SECRET and not healthy(obj, len(self.source['nodes'])):
                raise Refused('retained Calico workloads are not fully converged')
            objects[ref] = content(obj)
        return objects

    def verify_retained(self):
        if self.retained:
            if self.read_retained() != self.retained_objects:
                raise Refused('retained Calico object identity or configuration changed')
            self.verify_datastore()

    def planned_edges(self):
        if self.retained:
            self.verify_retained()
            return []
        stage = CalicoStage(self.binary, self.kubeconfig, self.source, self.variables,
            ('v3.28.4', 'v3.31.7'), run_id=self.variables['UPGRADE_RUN_ID'])
        obj = stage.read(['get', 'daemonset/calico-node', '-o', 'json'])
        containers = obj.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
        images = [c.get('image', '') for c in containers if c.get('name') == 'calico-node']
        if len(images) != 1 or images[0].split(':')[-1] not in ['v3.28.4', 'v3.31.7', 'v3.32.4']:
            raise Refused('Calico source image is not a reviewed exact version')
        version = images[0].split(':')[-1]
        edges = [['v3.28.4', 'v3.31.7'], ['v3.31.7', 'v3.32.4']]
        return edges if version == 'v3.28.4' else edges[1:] if version == 'v3.31.7' else []

    def book(self, name, extra=None):
        result = self.manager._run_playbook(self.cluster, PRODUCT/name,
            extra_vars={**self.variables, **(extra or {})})
        if type(result.rc) is not int or result.rc != 0:
            raise Refused('Calico host/client handoff failed; retain product runner diagnostics')

    def datastore(self):
        values = {}
        for resource in ['ippools', 'bgpconfigurations', 'networkpolicies', 'globalnetworkpolicies', 'hostendpoints']:
            result = subprocess.run([*self.etcd_args, 'get',
                '/calico/resources/v3/projectcalico.org/'+resource+'/', '--prefix', '--write-out=json'],
                capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise Refused('Calico datastore TLS read failed; no endpoint or credential fallback')
            data = json.loads(result.stdout)
            # JSON arrays reload as lists; keep first collection identical to
            # the durable snapshot, including when at least one resource exists.
            values[resource] = sorted([kv['key'], kv['value']] for kv in data.get('kvs', []))
        return values

    def verify_datastore(self, *, allow_created_default=False):
        actual = self.datastore()
        expected = deepcopy(self.original_datastore)
        saved = self.state/'calico-created-default-bgp.json'
        key = base64.b64encode(
            b'/calico/resources/v3/projectcalico.org/bgpconfigurations/default').decode()
        added = None
        if key not in dict(expected['bgpconfigurations']) and (allow_created_default or saved.exists()):
            value = dict(actual['bgpconfigurations']).get(key)
            if value is not None:
                try:
                    obj = content(json.loads(base64.b64decode(value, validate=True)))
                    obj['metadata'].pop('uid')
                except (ValueError, KeyError, TypeError):
                    raise Refused('new Calico default BGP configuration is invalid') from None
                # v3.32 node creates this missing object; Validate applies CRD
                # defaults even on direct etcd. No advertised ranges or mesh/AS
                # customization is part of this narrowly reviewed addition.
                if obj != {'kind': 'BGPConfiguration', 'apiVersion': 'projectcalico.org/v3',
                           'metadata': {'name': 'default'}, 'spec': {
                               'logSeverityScreen': 'Info',
                               'serviceLoadBalancerAggregation': 'Enabled'}}:
                    raise Refused('new Calico default BGP configuration has unreviewed content')
                added = [key, value]
                if saved.exists() and snapshot(saved, lambda: None) != added:
                    raise Refused('created Calico default BGP identity or content changed')
                expected['bgpconfigurations'] = sorted([*expected['bgpconfigurations'], added])
            elif saved.exists():
                raise Refused('created Calico default BGP configuration disappeared')
        if actual != expected:
            raise Refused('Calico pools/policies/host endpoints changed across the admitted edge')
        if added is not None and not saved.exists():
            snapshot(saved, lambda: added)

    def install_client(self, version):
        base = Path(self.variables['base_dir'])
        path = base/'extra-bin'/('calicoctl' if version == 'v3.32.4' else 'upgrade/calicoctl-v3.31.7')
        pins = json.loads(PINS_PATH.read_text())
        arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
        digest = pins['calicoctl'][version][arch]
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise Refused('Calico matching client differs from the admitted official bytes')
        self.book('calico-client.yml', {'UPGRADE_CALICOCTL_SOURCE': str(path),
            'UPGRADE_CALICOCTL_SHA256': digest, 'UPGRADE_CALICOCTL_VERSION': version})

    @property
    def steps(self):
        return ['platform/calico-'+edge[1] for edge in self.edges]

    def run(self, journal):
        self.verify_retained()
        if self.retained:
            self.traffic()
            return
        for step, edge in zip(self.steps, self.edges):
            if step in journal.data['completed']:
                continue
            stage = CalicoStage(self.binary, self.kubeconfig, self.source,
                                self.variables, edge, run_id=self.variables['UPGRADE_RUN_ID'])
            reviewed = snapshot(self.state/(step.split('/')[-1]+'.json'),
                lambda: {ref: stage.read(['get', ref, '-o', 'json']) for ref in [*stage.before, SECRET]})
            journal.begin(step)
            try:
                stage.run(reviewed, lambda: self.book('calico-qualify.yml'),
                          lambda: self.verify_datastore(allow_created_default=edge[1] == 'v3.32.4'),
                          self.traffic, self.install_client)
                snapshot(self.state/(edge[1]+'-accepted.json'),
                    lambda: {ref: stage.read(['get', ref, '-o', 'json'])
                             for ref in [*stage.after, SECRET]})
            except BaseException as error:
                journal.failed(step, getattr(error, 'rc', 1), 'kubernetes')
                raise
            journal.complete(step)
        stage = CalicoStage(self.binary, self.kubeconfig, self.source,
            self.variables, ('v3.31.7', 'v3.32.4'), run_id=self.variables['UPGRADE_RUN_ID'])
        accepted = snapshot(self.state/'v3.32.4-accepted.json',
            lambda: {ref: stage.read(['get', ref, '-o', 'json']) for ref in [*stage.after, SECRET]})
        declaration = stage.after if self.edges else manifests(self.variables, 'v3.32.4')
        if any(not matches(accepted.get(ref), declared) for ref, declared in declaration.items()):
            raise Refused('accepted Calico state differs from the target declaration')
        stage.wait(accepted)
        self.verify_datastore()
        self.traffic()
