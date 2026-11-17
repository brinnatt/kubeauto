"""Read-only admission for the 1.35 API removals and streaming RBAC change.

An enabled removed API or alpha DRA counter data needs a separately reviewed
migration. This module never deletes resources or broadens permissions.
"""
from upgrade.product.kubernetes_preflight import Refused, mapping, query


def admit(binary, config, server):
    def get(path):
        return mapping(query(binary, config, server, ['get', '--raw=' + path]))

    discovery = get('/apis')
    groups = discovery.get('groups')
    if not isinstance(groups, list):
        raise Refused('API discovery did not provide a complete group list')
    versions = {}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('versions'), list):
            raise Refused('API discovery group is incomplete')
        versions[group.get('name')] = {v.get('version') for v in group['versions']}
    for group in ['storagemigration.k8s.io', 'storage.k8s.io']:
        if 'v1alpha1' in versions.get(group, set()):
            raise Refused(f'removed {group}/v1alpha1 is served; migrate its resources and consumers before admission')

    # The certificates alpha group itself survives for ClusterTrustBundle.
    # Only PodCertificateRequest loses its alpha endpoint in 1.35, so inspect
    # resource discovery rather than rejecting every alpha certificate group.
    if 'v1alpha1' in versions.get('certificates.k8s.io', set()):
        resources=get('/apis/certificates.k8s.io/v1alpha1').get('resources')
        if not isinstance(resources,list) or any(not isinstance(r,dict) or not r.get('name') for r in resources):
            raise Refused('alpha certificate resource discovery is incomplete')
        if any(r['name'].split('/')[0]=='podcertificaterequests' for r in resources):
            raise Refused('removed certificates.k8s.io/v1alpha1/podcertificaterequests is served; migrate clients before admission')

    # DRA core is GA; the partitionable-devices alpha counter representation
    # changes incompatibly across this hop. Inspect persisted slices instead
    # of treating the presence of the GA API as a migration failure.
    dra_versions = versions.get('resource.k8s.io', set())
    if dra_versions:
        version = next((v for v in ['v1', 'v1beta1', 'v1alpha3'] if v in dra_versions), None)
        if version is None:
            raise Refused('DRA discovery has no reviewed resource-slice API')
        slices = get('/apis/resource.k8s.io/' + version + '/resourceslices')
        if not isinstance(slices.get('items'), list):
            raise Refused('DRA resource-slice observation is incomplete')
        for item in slices['items']:
            spec = item.get('spec', {})
            if spec.get('sharedCounters') or any(d.get('consumesCounters') for d in spec.get('devices', [])):
                raise Refused('alpha partitionable DRA counters require a separate migration before this hop')

    # Only explicit streaming grants are in question. A normal read-only Role
    # without these subresources does not acquire create permissions here.
    for path in ['/apis/rbac.authorization.k8s.io/v1/clusterroles',
                 '/apis/rbac.authorization.k8s.io/v1/roles']:
        roles = get(path)
        if not isinstance(roles.get('items'), list):
            raise Refused('streaming RBAC observation is incomplete')
        for role in roles['items']:
            rules = role.get('rules', [])
            for resource in ['pods/exec', 'pods/attach', 'pods/portforward']:
                relevant = [rule for rule in rules if '' in rule.get('apiGroups', [])
                            or '*' in rule.get('apiGroups', [])]
                explicit_get = any(resource in r.get('resources', []) and
                                   'get' in r.get('verbs', []) for r in relevant)
                create = any(('*' in r.get('resources', []) or resource in r.get('resources', []))
                             and ('create' in r.get('verbs', []) or '*' in r.get('verbs', []))
                             for r in relevant)
                if explicit_get and not create:
                    identity = role.get('metadata', {})
                    raise Refused('streaming RBAC needs create permission reviewed for ' +
                                  identity.get('namespace', '') + '/' + identity.get('name', 'unknown') +
                                  ': ' + resource)
