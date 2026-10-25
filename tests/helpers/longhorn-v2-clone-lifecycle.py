#!/usr/bin/env python3
"""Qualify full-copy independence after native retirement of its source.

The existing business journal also owns this fixture, so interruption recovery
uses the usual business-clean entry point. No shared installation is retired.
"""
import hashlib
import json
import re
import secrets

NS = 'longhorn-system'


def current(lab, kind, name, uid, namespace=''):
    obj = lab.get(kind, name, namespace)
    if obj and obj['metadata']['uid'] != uid:
        raise ValueError('full-copy lifecycle object replaced: '+kind+'/'+name)
    return obj


def delete(lab, kind, name, uid, namespace=''):
    obj = current(lab, kind, name, uid, namespace)
    if obj:
        lab.remove(obj)
    lab.wait('full-copy-retire-'+kind+'-'+name, 240,
             lambda: current(lab, kind, name, uid, namespace) is None)


def retire_source(lab, journal):
    """Remove only the recorded source; the independent clone stays mounted."""
    j = journal
    lab.check_business_graph(j)
    identities = (('pod', 'writer', j['objects']['Pod/writer'], j['namespace']),
                  ('pvc', 'data', j['objects']['PersistentVolumeClaim/data'], j['namespace']),
                  ('pv', j['pv'], j['pv_uid'], ''),
                  ('volumes.longhorn.io', j['volume'], j['volume_uid'], NS))
    # Reject a replaced object anywhere in the source graph before deleting
    # even its client. remove() supplies native UID/resourceVersion guards.
    if any(current(lab, *row) is None for row in identities):
        raise ValueError('full-copy source graph is incomplete before retirement')
    pv = current(lab, 'pv', j['pv'], j['pv_uid'])
    if (pv['spec']['claimRef'].get('uid') != j['objects']['PersistentVolumeClaim/data'] or
            pv['spec'].get('csi', {}).get('volumeHandle') != j['volume'] or
            pv['spec'].get('persistentVolumeReclaimPolicy') != 'Retain'):
        raise ValueError('full-copy source binding differs before retirement')
    for row in identities[:3]:
        delete(lab, *row)
    if any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == j['volume']
           for p in lab.get('pv')['items']):
        raise ValueError('another PV still refers to the full-copy source')

    def detached():
        volume = current(lab, 'volumes.longhorn.io', j['volume'], j['volume_uid'], NS)
        if not volume:
            raise ValueError('full-copy source disappeared before native retirement')
        return volume if volume.get('status', {}).get('state') == 'detached' else None
    lab.wait('full-copy-source-detached', 240, detached)
    delete(lab, *identities[3])
    lab.wait('full-copy-source-replicas-retired', 240, lambda: not any(
        r['spec'].get('volumeName') == j['volume']
        for r in lab.get('replicas.longhorn.io', namespace=NS)['items']))


def clone_state(lab, journal, *, size=2*1024**3):
    row = journal['claims']['clone']
    claim = current(lab, 'pvc', 'clone', journal['objects']['PersistentVolumeClaim/clone'], journal['namespace'])
    pv = current(lab, 'pv', row['pv'], row['pv_uid'])
    volume = current(lab, 'volumes.longhorn.io', row['volume'], row['volume_uid'], NS)
    if (not claim or not pv or not volume or claim['spec'].get('volumeName') != row['pv'] or
            pv['spec'].get('csi', {}).get('volumeHandle') != row['volume'] or
            pv['spec'].get('claimRef', {}).get('uid') != claim['metadata']['uid'] or
            volume['spec'].get('dataEngine') != 'v2' or volume['spec'].get('cloneMode') != 'full-copy' or
            volume.get('status', {}).get('cloneStatus', {}).get('state') != 'completed' or
            int(volume['spec']['size']) != size):
        raise ValueError('independent full-copy clone identity, binding or completed state changed')
    replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
                if r['spec'].get('volumeName') == row['volume']]
    if any(r['spec'].get('linkedCloneSrcReplicaName') for r in replicas):
        raise ValueError('full-copy clone retains a source replica dependency')
    return lab.replica_topology(row, 3) if lab.healthy_replicas(row['volume']) else None


def io(lab, journal, pod, value, *, read=False):
    uid = journal['objects']['Pod/'+pod]
    if not current(lab, 'pod', pod, uid, journal['namespace']):
        raise ValueError('full-copy lifecycle client disappeared')
    actual = lab.kubectl('-n', journal['namespace'], 'exec', pod, '--', 'python3', '-c',
                         lab.payload_program(value, read_only=read)).stdout.strip()
    expected = hashlib.sha256(value.encode()*262144).hexdigest()
    if actual != expected:
        raise ValueError('full-copy lifecycle application bytes differ')
    return expected


def business(lab, relay_pid):
    lab.run_requests(relay_pid, [])
    lab.installation_health()
    record = lab.DIRECTORY/'longhorn-install-business.json'
    if record.exists() or record.is_symlink():
        raise ValueError('previous full-copy business requires scoped cleanup')
    sc = lab.get('storageclass', 'longhorn')
    if (not sc or sc.get('parameters', {}).get('dataEngine') != 'v2' or
            sc.get('parameters', {}).get('cloneMode', 'full-copy') != 'full-copy' or
            sc.get('reclaimPolicy') != 'Retain' or
            sc['metadata'].get('labels', {}).get('kubeauto.io/component') != 'longhorn'):
        raise ValueError('full-copy lifecycle requires the owned V2 Retain class')
    name = 'longhorn-business-'+secrets.token_hex(8)
    ns = json.loads(lab.kubectl('create', 'namespace', name, '-o', 'json').stdout)
    j = {'namespace': name, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
         'storage_class': 'longhorn', 'reclaim_policy': 'Retain'}
    lab.write_private(record, j)
    def create(obj):
        return lab.create_business_object(j, record, obj)
    def client(pod, claim, index):
        create(lab.client_pod(name, pod, claim, 'longhorn-node-'+lab.WORKERS[index].rsplit('.', 1)[1]))
        def ready():
            obj = current(lab, 'pod', pod, j['objects']['Pod/'+pod], name)
            return obj and any(c['type'] == 'Ready' and c['status'] == 'True'
                               for c in obj.get('status', {}).get('conditions', []))
        lab.wait('full-copy-client-'+pod, 600, ready)
    with lab.scoped_cleanup(lambda: lab.cleanup_business(record, relay_pid=relay_pid)):
        j['defaults'] = lab.wait('full-copy-namespace-defaults', 60, lambda: lab.namespace_defaults(name))
        lab.write_private(record, j)
        claim = lab.clone_claim(name, 'data', '2Gi'); del claim['spec']['dataSource']
        create(claim)
        def bound():
            lab.bind_business_volume(j, record)
            claim = current(lab, 'pvc', 'data', j['objects']['PersistentVolumeClaim/data'], name)
            return j.get('volume_uid') and claim and claim.get('status', {}).get('phase') == 'Bound'
        lab.wait('full-copy-source-bound', 600, bound)
        client('writer', 'data', 0)
        value = lab.owner()['run_id']+'-independent'
        digest = io(lab, j, 'writer', value)
        lab.wait('full-copy-source-healthy', 600, lambda: lab.healthy_replicas(j['volume']))
        lab.verify_clone(j, create, value, digest)
        proof = lab.load_helper('longhorn-v2-clones.py').full_copy(lab, j, value, digest)
        before = lab.wait('full-copy-independent-baseline', 600, lambda: clone_state(lab, j))
        retire_source(lab, j)
        io(lab, j, 'clone-reader', value+'-clone', read=True)
        delete(lab, 'pod', 'clone-reader', j['objects']['Pod/clone-reader'], name)
        client('independent-reader', 'clone', 2)
        io(lab, j, 'independent-reader', value+'-clone', read=True)
        written = io(lab, j, 'independent-reader', value+'-after-source-delete')
        after = lab.wait('full-copy-independent-healthy', 600, lambda: clone_state(lab, j))
        if before != after:
            raise ValueError('full-copy clone replicas changed during source retirement/remount')
        if lab.get('volumes.longhorn.io', j['volume'], NS) or lab.get('pv', j['pv']):
            raise ValueError('full-copy source reappeared after retirement')
        print('LONGHORN_V2_FULL_COPY_SOURCE_RETIRED_RESULT '+json.dumps({
            'source_uid': j['volume_uid'], 'clone_uid': j['claims']['clone']['volume_uid'],
            'clone_pvc_uid': j['objects']['PersistentVolumeClaim/clone'],
            'clone_pv_uid': j['claims']['clone']['pv_uid'], 'replicas': after,
            'before_sha256': proof['clone_sha256'], 'after_sha256': written,
            'source_volume_absent': True, 'source_pv_absent': True,
            'cross_node_readback': True}, sort_keys=True), flush=True)
    lab.installation_health()
    print('LONGHORN_V2_FULL_COPY_SOURCE_RETIRED_BUSINESS_PASS', flush=True)


def verify_log(log):
    rows = [json.loads(s.split(' ', 1)[1]) for s in log.splitlines()
            if s.startswith('LONGHORN_V2_FULL_COPY_SOURCE_RETIRED_RESULT ')]
    if len(rows) != 1:
        raise ValueError('full-copy lifecycle lacks one complete result')
    row = rows[0]
    if (not all(row.get(k) for k in ('source_uid', 'clone_uid', 'clone_pvc_uid', 'clone_pv_uid')) or
            row['source_uid'] == row['clone_uid'] or
            not all(row.get(k) is True for k in ('source_volume_absent', 'source_pv_absent', 'cross_node_readback')) or
            not all(re.fullmatch('[a-f0-9]{64}', row.get(k, '')) for k in ('before_sha256', 'after_sha256')) or
            row['before_sha256'] == row['after_sha256'] or len(row['replicas']) != 3 or
            len({r['node'] for r in row['replicas'].values()}) != 3 or
            len({r['uid'] for r in row['replicas'].values() if r['uid']}) != 3):
        raise ValueError('full-copy lifecycle lacks independent identity, data or healthy placement')
    for marker in ('LONGHORN_V2_FULL_COPY_SOURCE_RETIRED_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('full-copy lifecycle lacks business or scoped cleanup result')
    if re.search(r'^LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)', log, re.M):
        raise ValueError('full-copy lifecycle contains a failure marker')
    return row
