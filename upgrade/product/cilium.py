"""Internal first-hop Cilium contracts; callers own admission and execution.

This module does not call the destructive installation role, mutate a cluster,
or certify a parent matrix item. Inputs must come from current API/Helm reads.
"""
from copy import deepcopy


CHARTS = {
    '1.19.10': 'b7252afca3413a33e2b987c954e52441e96979e411e1495ecc6aefe8cd258863',
    '1.20.4': '505abff8385811df1a949776fc42e9f10697989a332acc86d6e8933efcb855ed',
}
EDGES = {('1.19.5', '1.19.10'), ('1.19.10', '1.20.4')}


class CiliumRefused(ValueError):
    """A required upstream or project boundary is not proven."""


def at(value, path, default=None):
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def prepare_values(current, reviewed, source, target, *, preflight=False):
    """Preserve reviewed user values, replacing only the official image pairings.

    `reviewed` is the rendered product configuration bound by the outer plan,
    not Helm's computed defaults. A live override requires its own review; it
    must never be silently discarded or accepted via --reuse-values.
    """
    if (source, target) not in EDGES:
        raise CiliumRefused('only 1.19.5 -> 1.19.10 -> 1.20.4 is admitted')
    if not isinstance(current, dict) or current != reviewed:
        raise CiliumRefused('live Helm user values differ from the reviewed configuration')
    required = {'identityAllocationMode': 'crd', 'ipam.mode': 'cluster-pool',
                'etcd.enabled': False, 'envoy.enabled': False,
                'preflight.enabled': False}
    if any(at(current, path) != value for path, value in required.items()):
        raise CiliumRefused('identity, IPAM, embedded proxy or preflight mode differs')
    if any(at(current, path, False) is not False for path in ['cleanState', 'cleanBpfState']):
        raise CiliumRefused('datapath state cleanup is forbidden during rolling upgrade')
    # These paths require separate CRD/feature migrations and are not enabled
    # by the existing product configuration. Refuse rather than rewrite them.
    unsupported = ['gatewayAPI.enabled', 'ingressController.enabled',
                   'clustermesh.useAPIServer', 'clustermesh.apiserver.enabled',
                   'clustermesh.mcsapi.enabled', 'clustermesh.enableMCSAPISupport',
                   'authentication.mutual.spire.enabled', 'encryption.enabled']
    if any(at(current, path, False) is not False for path in unsupported):
        raise CiliumRefused('additional feature migration requires an explicit reviewed edge')
    if at(current, 'kubeProxyReplacement', 'false') not in (False, 'false'):
        raise CiliumRefused('this product path retains kube-proxy')
    if at(current, 'cni.configMap') or at(current, 'cni.configMapKey') or at(current, 'cni.customConf', False):
        raise CiliumRefused('custom CNI configuration needs a separate CNI 1.0 review')
    if at(current, 'hubble.tls.auto.method', 'helm') != 'helm':
        raise CiliumRefused('non-Helm certificate generation has additional artifact requirements')
    if at(current, 'image.tag') != 'v' + source or at(current, 'operator.image.tag') != 'v' + source:
        raise CiliumRefused('reviewed agent/operator versions must match the observed source')
    if at(current, 'hubble.relay.enabled', False) and at(current, 'hubble.relay.image.tag') != 'v' + source:
        raise CiliumRefused('relay must match the observed source')
    for path, expected in [('image.repository', 'cilium'),
                           ('operator.image.repository', 'cilium-operator')]:
        if at(current, path) != 'registry.talkschool.cn:5000/brinnatt/' + expected:
            raise CiliumRefused('image repository differs from the product delivery path')
    for path in ['image', 'operator.image', 'hubble.relay.image',
                 'hubble.ui.backend.image', 'hubble.ui.frontend.image']:
        image = at(current, path, {})
        if image.get('override') or image.get('digest') or image.get('useDigest', False) is not False:
            raise CiliumRefused('custom image override or digest needs its own artifact mapping review')
    values = deepcopy(current)
    values['upgradeCompatibility'] = '1.19'
    values['cleanState'] = False
    values['cleanBpfState'] = False
    for image in [values['image'], values['operator']['image']]:
        image.update(tag='v' + target, useDigest=False)
        image.pop('override', None)
        image.pop('digest', None)
    hubble = values.get('hubble', {})
    if at(hubble, 'relay.enabled', False):
        if at(hubble, 'relay.image.repository') != 'registry.talkschool.cn:5000/brinnatt/hubble-relay':
            raise CiliumRefused('relay repository differs from the product delivery path')
        hubble['relay']['image'].update(tag='v' + target, useDigest=False)
    if at(hubble, 'ui.enabled', False):
        for side, repository in [('backend', 'hubble-ui-backend'), ('frontend', 'hubble-ui')]:
            if at(hubble, f'ui.{side}.image.repository') != 'registry.talkschool.cn:5000/brinnatt/' + repository:
                raise CiliumRefused('UI repository differs from the product delivery path')
            hubble['ui'][side]['image'].update(tag='v0.13.6', useDigest=False)
    if preflight:
        # preflight.image is independent of image in both upstream Charts.
        values['agent'] = False
        values['operator']['enabled'] = False
        values['hubble'] = {'enabled': False}
        values['preflight'] = {'enabled': True, 'image': deepcopy(values['image'])}
    return values


def validate_removed_policy_rules(policy_lists):
    """Inspect both scoped policy APIs without modifying customer rules."""
    expected = {'CiliumNetworkPolicyList', 'CiliumClusterwideNetworkPolicyList'}
    if (not isinstance(policy_lists, list) or len(policy_lists) != 2 or
            {p.get('kind') for p in policy_lists if isinstance(p, dict)} != expected):
        raise CiliumRefused('both namespaced and clusterwide policy reads are required')
    for listing in policy_lists:
        if not isinstance(listing.get('items'), list):
            raise CiliumRefused('policy API list is incomplete')
        for policy in listing['items']:
            specs = ([policy['spec']] if 'spec' in policy else []) + policy.get('specs', [])
            for spec in specs:
                for direction in ['ingress', 'egress']:
                    for rule in spec.get(direction, []):
                        for port in rule.get('toPorts', []):
                            if {'kafka', 'l7', 'l7proto'} & port.get('rules', {}).keys():
                                raise CiliumRefused('removed proxylib policy rules require an explicit policy decision')


def validate_preflight(agent, preflight, validator, agent_pods, preflight_pods):
    """Require converged official DS/deployment and per-node image readiness."""
    for obj, kind, name in [(agent, 'DaemonSet', 'cilium'),
                            (preflight, 'DaemonSet', 'cilium-pre-flight-check'),
                            (validator, 'Deployment', 'cilium-pre-flight-check')]:
        if (obj.get('kind') != kind or at(obj, 'metadata.name') != name or
                at(obj, 'metadata.namespace') != 'kube-system' or
                not at(obj, 'metadata.uid') or
                at(obj, 'status.observedGeneration', -1) < at(obj, 'metadata.generation', 0)):
            raise CiliumRefused('preflight object identity or observed generation differs')
    count = at(agent, 'status.desiredNumberScheduled', 0)
    if type(count) is not int or count < 1:
        raise CiliumRefused('a nonempty healthy agent baseline is required')
    for ds in [agent, preflight]:
        if (any(at(ds, 'status.' + key, -1) != count for key in
                ['desiredNumberScheduled', 'currentNumberScheduled', 'numberReady',
                 'updatedNumberScheduled', 'numberAvailable']) or
                at(ds, 'status.numberMisscheduled', 0) != 0):
            raise CiliumRefused('preflight must cover every converged Cilium agent')
    if (at(validator, 'spec.replicas') != 1 or any(at(validator, 'status.' + key, 0) != 1
            for key in ['replicas', 'readyReplicas', 'updatedReplicas', 'availableReplicas'])):
        raise CiliumRefused('official CNP validator deployment must converge to 1/1')

    def ready_nodes(pods, owner):
        if not isinstance(pods, dict) or not isinstance(pods.get('items'), list):
            raise CiliumRefused('Pod list is incomplete')
        nodes = []
        for pod in pods['items']:
            if (at(pod, 'metadata.namespace') != 'kube-system' or
                    at(pod, 'metadata.deletionTimestamp') or
                    not any(ref.get('uid') == at(owner, 'metadata.uid') and
                            ref.get('controller') is True for ref in
                            at(pod, 'metadata.ownerReferences', [])) or
                    at(pod, 'status.phase') != 'Running' or
                    not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                            for c in at(pod, 'status.conditions', [])) or
                    not at(pod, 'spec.nodeName')):
                raise CiliumRefused('each pre-pull Pod must be ready and owned by its DaemonSet')
            nodes.append(pod['spec']['nodeName'])
        if len(nodes) != count or len(set(nodes)) != count:
            raise CiliumRefused('pre-pull Pod count or placement differs')
        return set(nodes)

    if ready_nodes(agent_pods, agent) != ready_nodes(preflight_pods, preflight):
        raise CiliumRefused('pre-pull must be ready on the actual Cilium nodes')
