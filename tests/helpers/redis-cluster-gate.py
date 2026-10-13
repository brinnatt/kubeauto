#!/usr/bin/env python3
"""Separate product Cluster fixture; reuse the owned Operator and core, preserve Sentinel."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import tempfile
import time

import yaml

from common.constants import KubeConstant

FIXTURE = 'redis-cluster-gate'
NAME = 'redis-gate-cluster-data'
TLS_SECRET = 'redis-gate-cluster-tls'


def cluster_config(source):
    if source.get('redis_namespace') != 'redis-gate' or source.get('redis_mode') != 'sentinel':
        raise ValueError('Cluster fixture requires the healthy owned Sentinel source config')
    return dict(source, CLUSTER_NAME=FIXTURE, redis_name=NAME, redis_mode='cluster',
                redis_tls_secret=TLS_SECRET, redis_pvc_size='2Gi',
                redis_cluster_image_ver=KubeConstant().v_redis_cluster_image,
                redis_cluster_shards=3, redis_cluster_replicas=2,
                redis_snapshot_requested=False, redis_backup_enabled=False)


def inventory_for_cluster(text):
    old = 'cluster_dir="{{ base_dir }}/clusters/redis-gate"'
    if text.count(old) != 1:
        raise ValueError('Cluster fixture refuses an unexplained source inventory path')
    return text.replace(old, 'cluster_dir="{{ base_dir }}/clusters/redis-cluster-gate"')


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def owned_metadata(obj, namespace, *, uid=None, app=None):
    metadata = obj['metadata']
    labels = metadata.get('labels', {})
    if (metadata.get('namespace') != namespace or (uid and metadata['uid'] != uid) or
            labels.get('kubeauto.io/managed-by') != 'kubeauto' or
            labels.get('kubeauto.io/component') != 'redis' or (app and labels.get('app') != app)):
        raise ValueError('Cluster cleanup refuses changed object identity or ownership')
    return metadata


def delete_exact(gate, obj, resource):
    metadata = obj['metadata']
    gate.k('delete', '--raw', f"/api/v1/namespaces/{gate.ns}/{resource}/{metadata['name']}",
           '-f', '-', input_text=json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions',
           'preconditions': {'uid': metadata['uid'], 'resourceVersion': metadata['resourceVersion']}}))


def cleanup_actions(actions, failure):
    errors = []
    for action in actions:
        try:
            action()
        except Exception as exc:
            errors.append(exc)
    if errors:
        if failure is None:
            raise errors[0]
        print('REDIS_CLUSTER_CLEANUP_FAIL first failure retained; fixture journal retained', flush=True)


def cleanup_fixture(gate, target, journal, state):
    if (target != gate.base / 'clusters' / FIXTURE or json.loads(journal.read_text())['run_id'] != state['run_id'] or
            target.is_symlink()):
        raise ValueError('Cluster cleanup refuses changed fixture directory or journal')
    owner_path = target / 'redis-fixture-owner.json'
    if not owner_path.is_file() or json.loads(owner_path.read_text()) != {'run_id': state['run_id']}:
        raise ValueError('Cluster cleanup requires its private product directory marker')
    if hashlib.sha256((target / 'config.yml').read_bytes()).hexdigest() != state['config_sha256']:
        raise ValueError('Cluster cleanup refuses concurrently changed product input')
    raw = gate.k('-n', gate.ns, 'get', 'rediscluster', NAME, '--ignore-not-found', '-o', 'json')
    cr = json.loads(raw) if raw else None
    if cr:
        metadata = owned_metadata(cr, gate.ns, uid=state.get('cr_uid'))
        if metadata['name'] != NAME or cr['spec']['TLS']['secret']['secretName'] != TLS_SECRET:
            raise ValueError('Cluster cleanup refuses a different declaration')
    claims = []
    for claim in gate.get('pvc')['items']:
        app = claim['metadata'].get('labels', {}).get('app')
        if app not in (NAME + '-leader', NAME + '-follower'):
            continue
        owned_metadata(claim, gate.ns, app=app)
        if not (claim['metadata']['name'].startswith(app + '-') or
                claim['metadata']['name'].startswith('node-conf-' + app + '-')):
            raise ValueError('Cluster cleanup refuses an unrelated claim name')
        claims.append(claim)
    tls = gate.get('secret', TLS_SECRET)
    labels = tls['metadata'].get('labels', {})
    if (tls['metadata']['uid'] != state['tls_uid'] or labels.get('kubeauto.io/test-run') != state['run_id'] or
            labels.get('kubeauto.io/component') != 'redis-test' or labels.get('kubeauto.io/managed-by') != 'kubeauto'):
        raise ValueError('Cluster cleanup refuses a foreign TLS Secret')
    raw_config = gate.k('-n', gate.ns, 'get', 'configmap', NAME + '-config', '--ignore-not-found', '-o', 'json')
    config = json.loads(raw_config) if raw_config else None
    if config:
        owned_metadata(config, gate.ns)
    # All deletion boundaries are proven before the first mutation; keep the
    # shared Operator alive so the CR finalizer can finish normally.
    if cr:
        gate.k('-n', gate.ns, 'delete', 'rediscluster', NAME, '--wait=true', '--timeout=5m', timeout=330)
    for claim in claims:
        raw_claim = gate.k('-n', gate.ns, 'get', 'pvc', claim['metadata']['name'], '--ignore-not-found', '-o', 'json')
        if raw_claim:
            current = json.loads(raw_claim)
            owned_metadata(current, gate.ns, uid=claim['metadata']['uid'], app=claim['metadata']['labels']['app'])
            delete_exact(gate, current, 'persistentvolumeclaims')
    delete_exact(gate, tls, 'secrets')
    if config:
        delete_exact(gate, config, 'configmaps')
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        remaining = [p for p in gate.get('pvc')['items'] if p['metadata']['name'] in
                     {c['metadata']['name'] for c in claims}]
        pods = [p for p in gate.get('pods')['items'] if p['metadata'].get('labels', {}).get('app') in
                (NAME + '-leader', NAME + '-follower')]
        if not remaining and not pods:
            break
        print('REDIS_CLUSTER_CLEAN_HEARTBEAT owned-pvcs-or-pods=terminating', flush=True)
        time.sleep(3)
    else:
        raise ValueError('Cluster scoped cleanup has remaining owned resources')
    shutil.rmtree(target)
    journal.unlink()
    gate.health()
    print('REDIS_CLUSTER_FIXTURE_CLEAN_PASS sentinel=healthy cluster-pods-and-pvcs=absent shared-operator=retained', flush=True)


def seed_failure_state(gate):
    """Only the pre-fault seed stage permits scoped Cluster disposal recovery."""
    path = gate.cluster / 'redis-stability-fault.json'
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError('Cluster cleanup refuses a foreign stability journal')
    state = json.loads(path.read_text())
    fixture = json.loads((gate.cluster / 'redis-cluster-fixture.json').read_text())
    owner = json.loads((gate.cluster / 'redis-lab-owner.json').read_text())
    if (set(state) != {'run_id', 'cluster_uid', 'cr_uid', 'stage'} or state['stage'] != 'seed' or
            not re.fullmatch(r'[0-9a-f]{32}', state['run_id']) or
            state['cluster_uid'] != owner['cluster_uid'] or
            state['cluster_uid'] != fixture['cluster_uid'] or state['cr_uid'] != fixture['cr_uid'] or
            gate.get('rediscluster', NAME)['metadata']['uid'] != state['cr_uid'] or
            any(p != path for pattern in ('*fault*.json', '*fault*.tmp') for p in gate.cluster.glob(pattern)) or
            gate.get('networkpolicy')['items']):
        raise ValueError('Cluster cleanup requires an owned seed-only failure with no injected faults')
    return state


def clean_owned_fixture(gate):
    """Remove a failed, owned Cluster boundary while retaining source business."""
    target = gate.base / 'clusters' / FIXTURE
    journal = gate.cluster / 'redis-cluster-fixture.json'
    state = json.loads(journal.read_text())
    owner = json.loads((gate.cluster / 'redis-lab-owner.json').read_text())
    if (state['cluster_uid'] != owner['cluster_uid'] or state['name'] != NAME or
            state['namespace'] != gate.ns):
        raise ValueError('Cluster reset refuses a different lab/declaration identity')
    seed = seed_failure_state(gate)
    if any(gate.cluster.glob('redis-cluster-process-fault-*.json')) or (
            gate.cluster / 'redis-cluster-ask-fault.json').exists():
        raise ValueError('Cluster reset requires process and ASK fault recovery first')
    fault = gate.cluster / 'redis-cluster-scale-fault.json'
    if fault.exists():
        mutation = json.loads(fault.read_text())
        if (mutation['cr_uid'] != state['cr_uid'] or
                mutation['original_config_sha256'] != state['config_sha256']):
            raise ValueError('Cluster reset refuses an unproven scale fault')
    def source_identities():
        result = {kind: gate.focused.resource_identities(gate.get(kind)['items'])
                  for kind in ('redisreplication', 'redissentinel')}
        result['pvc'] = gate.focused.resource_identities([obj for obj in gate.get('pvc')['items']
            if obj['metadata'].get('labels', {}).get('app') == gate.name])
        return result
    preserved = source_identities()
    failure = None
    try:
        primary, client = gate.seeded_primary()
        client.command('EXPIRE', gate.key, 7200)
        cleanup_fixture(gate, target, journal, state)
        fault.unlink(missing_ok=True)
        (gate.cluster / 'redis-cluster-failure-keys.json').unlink(missing_ok=True)
        gate.health()
        if gate.client(gate.primary()).command('GET', gate.key) != gate.value:
            raise ValueError('Cluster cleanup changed source Sentinel business data')
        if preserved != source_identities():
            raise ValueError('Cluster cleanup replaced source declarations or PVCs')
        if seed:
            key = 'kubeauto:redis:focused:' + seed['run_id']
            client = gate.client(gate.primary())
            client.command('DEL', key)
            if client.command('EXISTS', key) != 0:
                raise ValueError('Cluster cleanup retained the failed seed key')
            path = gate.cluster / 'redis-stability-fault.json'
            if json.loads(path.read_text()) != seed:
                raise ValueError('Cluster cleanup refuses a concurrently changed seed journal')
            path.unlink()
            print('REDIS_STABILITY_SEED_RECOVERY_PASS faults=none owned-key=absent cluster=removed', flush=True)
        print('REDIS_CLUSTER_FAILED_FIXTURE_RECOVERY_PASS source-data=retained owned-cluster=absent', flush=True)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        cleanup_actions([gate.cleanup_keys], failure)


def verify_startup_config(gate, pod, client, config):
    """Prove actual init/data mounts, generated include, and effective settings."""
    external = next((v for v in pod['spec']['volumes'] if v['name'] == 'external-config'), None)
    if not external or external.get('configMap', {}).get('name') != NAME + '-config':
        raise ValueError('Cluster member lacks its declared startup ConfigMap volume')
    containers = [pod['spec']['containers'][0], next(c for c in pod['spec']['initContainers']
                                                   if c['name'] == 'init-config')]
    for container in containers:
        if not any(m['name'] == 'external-config' and
                   m['mountPath'] == '/etc/redis/external.conf.d'
                   for m in container.get('volumeMounts', [])):
            raise ValueError('Cluster init/data container lacks startup config mount')
    expected = {'cluster-migration-barrier': str(config['redis_cluster_replicas']),
                'appendonly': 'yes', 'appendfsync': config['redis_appendfsync'],
                'min-replicas-to-write': str(config['redis_min_replicas_to_write']),
                'min-replicas-max-lag': str(config['redis_min_replicas_max_lag']),
                'cluster-require-full-coverage': 'yes', 'tls-auth-clients': 'yes'}
    for key, value in expected.items():
        if client.command('CONFIG', 'GET', key) != [key.encode(), value.encode()]:
            raise ValueError('Cluster effective startup setting differs: ' + key)
    # Never read the generated file into Python/logs: it contains credentials.
    gate.k('-n', gate.ns, 'exec', pod['metadata']['name'], '-c', containers[0]['name'], '--',
           'sh', '-ec', 'grep -Fx -- "$1" /etc/redis/redis.conf >/dev/null', 'verify-include',
           'include /etc/redis/external.conf.d/redis-additional.conf')


def startup_members_ready(groups, init_image=None):
    """Old Ready Pods are not evidence of the newly declared role mounts."""
    if {role: len(pods) for role, pods in groups.items()} != {'leader': 3, 'follower': 6}:
        return False
    for pods in groups.values():
        for pod in pods:
            status = pod.get('status', {})
            if (pod['metadata'].get('deletionTimestamp') or not status.get('podIP') or
                    not any(c['type'] == 'Ready' and c['status'] == 'True'
                            for c in status.get('conditions', [])) or
                    not any(v.get('configMap', {}).get('name') == NAME + '-config' and
                            v['name'] == 'external-config' for v in pod['spec'].get('volumes', []))):
                return False
            if init_image and not any(c.get('name') == 'init-config' and c.get('image') == init_image
                                      for c in pod['spec'].get('initContainers', [])):
                return False
    return True


def wait_startup_rollout(cluster, init_image=None):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if startup_members_ready(cluster.members(), init_image):
            cluster.source.forwards.clear()
            cluster.health()
            return
        print('REDIS_CLUSTER_CONFIG_HEARTBEAT state=waiting-for-new-role-pods', flush=True)
        time.sleep(5)
    raise ValueError('Cluster role startup configuration rollout did not converge')


def reconcile_startup_config(gate, focused, work):
    clients = load(gate.base, 'redis-cluster-client-gate.py', 'redis_cluster_startup_clients')
    cluster = clients.Cluster(gate, focused, work)
    cluster.health()
    config = yaml.safe_load((cluster.target / 'config.yml').read_text())
    init_image = 'registry.talkschool.cn:5000/brinnatt/redis-operator:' + config.get(
        'redis_operator_image_ver', '0.27.0-kubeauto.2')
    before = focused.resource_identities(cluster.get('pvc')['items'])
    failure = None
    try:
        for i in range(32):
            key = 'kubeauto:redis:cluster:' + cluster.run_id + '{startup-' + str(i) + '}'
            cluster.keys.add(key)
            if cluster.command(key, 'SET', key, gate.value, 'EX', 7200) != b'OK':
                raise ValueError('Cluster startup convergence probe rejected')
        routes = {key: cluster.owner(key) for key in cluster.keys}
        for pod in set(routes.values()):
            key = next(key for key, owner in routes.items() if owner == pod)
            client = cluster.client(pod, fresh=True)
            client.command('SET', key, gate.value, 'EX', 7200)
            if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                raise ValueError('Cluster startup probe lacks durable replication')
        gate.run([str(gate.base / '.venv/bin/python'), str(gate.base / 'kubecli.py'),
                  'download', '-E', 'redis'], timeout=800, capture=False)
        gate.run([str(gate.base / '.venv/bin/python'), str(gate.base / 'kubecli.py'),
                  'setup', FIXTURE, '07', '--tags', 'redis'], timeout=1100, capture=False)
        wait_startup_rollout(cluster, init_image)
        operator = json.loads(gate.k('-n', gate.config['redis_operator_namespace'], 'get',
                                     'deployment', 'redis-operator', '-o', 'json'))
        if (operator['spec']['template']['spec']['containers'][0]['image'] != init_image or
                operator.get('status', {}).get('updatedReplicas') != 2):
            raise ValueError('Cluster configuration gate lacks the declared Operator image rollout')
        config = yaml.safe_load((cluster.target / 'config.yml').read_text())
        for group in cluster.members().values():
            for pod in group:
                verify_startup_config(gate, pod, cluster.client(pod['metadata']['name'], fresh=True), config)
        for key in cluster.keys:
            if cluster.command(key, 'GET', key) != gate.value or cluster.command(key, 'TTL', key) <= 0:
                raise ValueError('Cluster startup convergence lost business value/expiry')
        if before != focused.resource_identities(cluster.get('pvc')['items']):
            raise ValueError('Cluster startup convergence replaced a PVC')
        print('REDIS_CLUSTER_STARTUP_CONFIG_PASS members=9 init-and-data-mounts=verified '
              'generated-include=verified effective-settings=verified data-and-pvc-uids=retained', flush=True)
    except BaseException as exc:
        failure = exc
        try:
            cluster.fault_diagnostics()
        except Exception:
            print('REDIS_CLUSTER_DIAGNOSTICS_FAIL first failure retained', flush=True)
        raise
    finally:
        def cleanup():
            wait_startup_rollout(cluster, init_image)
            cluster.cleanup()
        cleanup_actions([cleanup], failure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('cluster-install', 'cluster-clean', 'cluster-config', 'cluster-config-repair'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_cluster_focused')
    fixtures = load(args.base, 'redis-lab-config.py', 'redis_cluster_fixtures')
    with tempfile.TemporaryDirectory(prefix='redis-cluster-install-') as temporary, ExitStack() as stack:
        gate = focused.Gate(args, Path(temporary), stack)
        if args.module == 'cluster-clean' and seed_failure_state(gate):
            gate.identity()
            gate.health()
            gate.run([str(args.base / '.venv/bin/python'),
                      str(args.base / 'tests/helpers/redis-lab-storage.py'), 'health', '--base', str(args.base)],
                     capture=False)
        else:
            gate.baseline()
        gate.focused = focused
        if args.module == 'cluster-clean':
            clean_owned_fixture(gate)
        elif args.module == 'cluster-config':
            reconcile_startup_config(gate, focused, Path(temporary))
        elif args.module == 'cluster-config-repair':
            clients = load(gate.base, 'redis-cluster-client-gate.py', 'redis_cluster_startup_repair')
            cluster = clients.Cluster(gate, focused, Path(temporary))
            wait_startup_rollout(cluster)
            cluster.recover_failure_keys(startup=True)
            cluster.cleanup()
        else:
            install_fixture(gate, fixtures, args, focused, Path(temporary), stack)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


def install_fixture(gate, fixtures, args, focused, work, stack):
    source = gate.cluster
    target = args.base / 'clusters' / FIXTURE
    journal = source / 'redis-cluster-fixture.json'
    if target.exists() or journal.exists() or gate.k('-n', gate.ns, 'get', 'rediscluster', NAME,
                                                    '--ignore-not-found', '-o', 'name'):
        raise ValueError('Cluster install requires a clean independent fixture boundary')
    if gate.k('-n', gate.ns, 'get', 'secret', TLS_SECRET, '--ignore-not-found', '-o', 'name'):
        raise ValueError('Cluster install refuses an existing TLS fixture Secret')
    for kind in ('pods', 'pvc', 'statefulsets', 'services'):
        if any(obj['metadata'].get('labels', {}).get('app') in (NAME + '-leader', NAME + '-follower')
               for obj in gate.get(kind)['items']):
            raise ValueError('Cluster install refuses preexisting member resources')
    if gate.k('-n', gate.ns, 'get', 'configmap', NAME + '-config', '--ignore-not-found', '-o', 'name'):
        raise ValueError('Cluster install refuses an existing configuration object')
    # Publication/readback does not populate the customer's internal registry.
    # Complete the documented product download before creating any fixture.
    gate.run([str(args.base / '.venv/bin/python'), str(args.base / 'kubecli.py'),
              'download', '-E', 'redis'], timeout=800, capture=False)
    preserved = {kind: focused.resource_identities(gate.get(kind)['items'])
                 for kind in ('redisreplication', 'redissentinel', 'pvc')}
    primary, client = gate.seeded_primary()
    client.command('EXPIRE', gate.key, 7200)
    if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
        raise ValueError('Cluster fixture source business boundary is not durable')
    # Write the ownership intent before any product/fixture mutation. A
    # failure is retained for explicit scoped recovery, not a healthy baseline.
    state = {'schema': 1, 'healthy': False, 'name': NAME, 'namespace': gate.ns,
             'cluster': FIXTURE, 'tls_secret': TLS_SECRET, 'run_id': gate.run_id,
             'cluster_uid': json.loads((source / 'redis-lab-owner.json').read_text())['cluster_uid']}
    with journal.open('x') as out:
        os.chmod(journal, 0o600)
        json.dump(state, out)
        out.flush()
        os.fsync(out.fileno())
    failure = None
    try:
        gate.run([str(args.base / '.venv/bin/python'), str(args.base / 'kubecli.py'), 'new', FIXTURE],
                 timeout=90, capture=False)
        config = cluster_config(gate.config)
        (target / 'config.yml').write_text(yaml.safe_dump(config, sort_keys=False))
        (target / 'config.yml').chmod(0o600)
        (target / 'redis-fixture-owner.json').write_text(json.dumps({'run_id': gate.run_id}))
        (target / 'redis-fixture-owner.json').chmod(0o600)
        state['config_sha256'] = hashlib.sha256((target / 'config.yml').read_bytes()).hexdigest()
        (target / 'hosts').write_text(inventory_for_cluster((source / 'hosts').read_text()))
        shutil.copyfile(gate.kubeconfig, target / 'kubectl.kubeconfig')
        (target / 'kubectl.kubeconfig').chmod(0o600)
        credentials = fixtures.credentials(NAME)
        tls = credentials[2]
        tls['metadata']['name'] = TLS_SECRET
        tls['metadata']['labels']['kubeauto.io/test-run'] = gate.run_id
        gate.k('create', '-f', '-', input_text=json.dumps(tls))
        state['tls_uid'] = gate.get('secret', TLS_SECRET)['metadata']['uid']
        journal.write_text(json.dumps(state, indent=2) + '\n')
        gate.run([str(args.base / '.venv/bin/python'), str(args.base / 'kubecli.py'),
                  'setup', FIXTURE, '07', '--tags', 'redis'], timeout=1100, capture=False)
        state['tls_uid'] = gate.get('secret', TLS_SECRET)['metadata']['uid']
        cr = gate.get('rediscluster', NAME)
        state['cr_uid'] = cr['metadata']['uid']
        cluster_pods = [p for p in gate.get('pods')['items'] if
                        p['metadata'].get('labels', {}).get('app') in (NAME + '-leader', NAME + '-follower')]
        image = 'registry.talkschool.cn:5000/brinnatt/redis:' + config['redis_cluster_image_ver']
        if (cr['spec']['kubernetesConfig']['image'] != image or len(cluster_pods) != 9 or
                any(not any(c['name'] == p['metadata']['labels']['app'] and c['image'] == image
                            for c in p['spec']['containers']) for p in cluster_pods)):
            raise ValueError('Cluster product installation lacks nine members on the declared runtime image')
        tls_directory = work / 'cluster-tls'
        tls_directory.mkdir(mode=0o700)
        for key, value in tls['stringData'].items():
            (tls_directory / key).write_text(value)
            (tls_directory / key).chmod(0o600)
        context = ssl.create_default_context(cafile=str(tls_directory / 'ca.crt'))
        context.load_cert_chain(str(tls_directory / 'tls.crt'), str(tls_directory / 'tls.key'))
        roles = []
        for pod in cluster_pods:
            hostname = f"{pod['spec']['hostname']}.{pod['spec']['subdomain']}.{gate.ns}.svc.cluster.local"
            cluster_client = focused.Client(gate.forward(pod['metadata']['name'], 6379), context,
                                            hostname, gate.passwords['password'])
            stack.callback(cluster_client.close)
            if cluster_client.command('PING') != b'PONG':
                raise ValueError('Cluster member authenticated hostname-verified TLS PING failed')
            if cluster_client.command('CONFIG', 'GET', 'cluster-migration-barrier') != [
                    b'cluster-migration-barrier', str(config['redis_cluster_replicas']).encode()]:
                raise ValueError('Cluster migration barrier differs from declared replica budget')
            verify_startup_config(gate, pod, cluster_client, config)
            roles.append(cluster_client.command('ROLE')[0])
        if roles.count(b'master') != 3 or roles.count(b'slave') != 6:
            raise ValueError('Cluster live roles do not match three primaries and six replicas')
        claims = [p for p in gate.get('pvc')['items'] if p['metadata']['name'] not in
                  {name for name, uid in preserved['pvc']}]
        if len(claims) != 18 or any(p['metadata'].get('labels', {}).get('app') not in
                                   (NAME + '-leader', NAME + '-follower') for p in claims):
            raise ValueError('Cluster installation lacks exact owned data/node-conf claim boundary')
        state['claims'] = focused.resource_identities(claims)
        gate.health()
        for kind, identities in preserved.items():
            current = focused.resource_identities(gate.get(kind)['items'])
            if any(item not in current for item in identities):
                raise ValueError('independent Cluster installation replaced a Sentinel source resource')
        if gate.client(gate.primary(), fresh=True).command('GET', gate.key) != gate.value:
            raise ValueError('independent Cluster install altered Sentinel business data')
        state['healthy'] = True
        journal.write_text(json.dumps(state, indent=2) + '\n')
        print('REDIS_CLUSTER_INSTALL_PASS shards=3 replicas-per-shard=2 members=9 slots=16384 '
              'tls-ping-and-real-roles=verified product-entry=verified sentinel-data-and-pvc-uids=retained', flush=True)
    except BaseException as exc:
        failure = exc
        try:
            gate.fault_diagnostics()
            print(gate.k('-n', gate.ns, 'get', 'rediscluster', NAME, '-o', 'json'), flush=True)
            print(gate.k('-n', gate.ns, 'get', 'events', '--sort-by=.lastTimestamp'), flush=True)
        except Exception:
            print('REDIS_CLUSTER_DIAGNOSTICS_FAIL first failure retained', flush=True)
        raise
    finally:
        actions = []
        if failure is not None and 'tls_uid' in state:
            actions.append(lambda: cleanup_fixture(gate, target, journal, state))
        actions.append(gate.cleanup_keys)
        cleanup_actions(actions, failure)


if __name__ == '__main__':
    main()
