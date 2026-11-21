"""Read-only admission for the 1.35 API removals and streaming RBAC change.

An enabled removed API or alpha DRA counter data needs a separately reviewed
migration. This module never deletes resources or broadens permissions.
"""
from upgrade.product.kubernetes_preflight import Refused, mapping, query


def admit(binary, config, server, *, target='v1.35.10'):
    if target not in ['v1.35.10', 'v1.36.6']:
        raise Refused('API admission requires an explicitly reviewed target')
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

    if target == 'v1.36.6':
        _admit_git_repo_and_dra(get, versions)


def _admit_git_repo_and_dra(get, versions):
    """Refuse persisted uses of changed APIs; never rewrite customer workloads."""
    paths = [('/api/v1/pods', ('spec',)),
             ('/api/v1/podtemplates', ('template', 'spec')),
             ('/api/v1/replicationcontrollers', ('spec', 'template', 'spec'))]
    paths += [('/apis/apps/v1/' + kind, ('spec', 'template', 'spec'))
              for kind in ['deployments', 'replicasets', 'daemonsets', 'statefulsets']]
    paths += [('/apis/batch/v1/jobs', ('spec', 'template', 'spec')),
              ('/apis/batch/v1/cronjobs', ('spec', 'jobTemplate', 'spec', 'template', 'spec'))]
    for path, fields in paths:
        items = get(path).get('items')
        if not isinstance(items, list):
            raise Refused('complete workload observation required before the gitRepo lock')
        for obj in items:
            spec = obj
            for field in fields:
                if not isinstance(spec, dict):
                    raise Refused('workload pod specification is incomplete')
                spec = spec.get(field)
            if not isinstance(spec, dict) or not isinstance(spec.get('volumes', []), list):
                raise Refused('workload volume observation is incomplete')
            if any(not isinstance(volume, dict) or 'gitRepo' in volume
                   for volume in spec.get('volumes', [])):
                raise Refused('gitRepo volumes cannot run on Kubernetes 1.36; migrate workloads before upgrade')

    # This product does not deliver DRA drivers. Existing nonempty claims/slices
    # require their owner's granular-status RBAC and driver qualification.
    dra = versions.get('resource.k8s.io', set())
    if dra:
        version = next((v for v in ['v1', 'v1beta1'] if v in dra), None)
        if version is None:
            raise Refused('DRA discovery has no reviewed 1.36 transition API')
        for resource in ['resourceclaims', 'resourceslices']:
            items = get('/apis/resource.k8s.io/' + version + '/' + resource).get('items')
            if not isinstance(items, list) or items:
                raise Refused('nonempty or unknown DRA resources require a separately qualified driver migration')
