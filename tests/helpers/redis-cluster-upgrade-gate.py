#!/usr/bin/env python3
"""Fixed-topology RedisCluster patch upgrade/rollback under the protected controller."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

import yaml
from common.constants import KubeConstant


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version_input(original, version):
    config = yaml.safe_load(original)
    if (config.get('redis_namespace') != 'redis-gate' or config.get('redis_mode') != 'cluster' or
            config.get('redis_cluster_shards') != 3 or config.get('redis_cluster_replicas') != 2 or
            config.get('redis_operator_image_ver') != '0.27.0-kubeauto.2' or
            version not in ('8.10.1', KubeConstant().v_redis_cluster_image)):
        raise ValueError('Cluster patch migration requires the fixed protected three-shard fixture')
    return yaml.safe_dump(dict(config, redis_cluster_image_ver=version), sort_keys=False).encode()


def members_ready(groups, image):
    pods = [p for group in groups.values() for p in group]
    return (len(groups.get('leader', [])) == 3 and len(groups.get('follower', [])) == 6 and
            all(not p['metadata'].get('deletionTimestamp') and
                any(c['type'] == 'Ready' and c['status'] == 'True' for c in p['status'].get('conditions', [])) and
                any(c['name'] == p['metadata']['labels']['app'] and c['image'] == image
                    for c in p['spec']['containers']) for p in pods))


def rollback_deletions(cluster, image):
    """Kubernetes forced rollback: only failed old Pods of restored owned templates."""
    name = 'redis-gate-cluster-data'
    cr = cluster.get('rediscluster', name)
    metadata = cr['metadata']; labels = metadata.get('labels', {})
    if (metadata['uid'] != cluster.journal['cr_uid'] or metadata.get('namespace') != cluster.ns or
            metadata.get('name') != name or labels.get('kubeauto.io/component') != 'redis' or
            labels.get('kubeauto.io/managed-by') != 'kubeauto'):
        raise ValueError('Cluster rollback refuses a changed declaration')
    groups = cluster.members()
    if {role: len(pods) for role, pods in groups.items()} != {'leader': 3, 'follower': 6}:
        raise ValueError('Cluster rollback requires its exact nine-member boundary')
    deletions = []
    templates_ready = cr['spec']['kubernetesConfig']['image'] == image
    for role, pods in groups.items():
        app = name + '-' + role
        sts = cluster.get('statefulset', app)
        owners = [o for o in sts['metadata'].get('ownerReferences', []) if o.get('controller')]
        if (sts['metadata'].get('namespace') != cluster.ns or len(owners) != 1 or
                owners[0].get('uid') != cluster.journal['cr_uid'] or
                owners[0].get('kind') != 'RedisCluster' or owners[0].get('name') != name):
            raise ValueError('Cluster rollback refuses a foreign StatefulSet')
        templates_ready = templates_ready and any(c['name'] == app and c['image'] == image
                for c in sts['spec']['template']['spec']['containers'])
        for pod in pods:
            metadata = pod['metadata']; labels = metadata.get('labels', {})
            owners = [o for o in metadata.get('ownerReferences', []) if o.get('controller')]
            if (metadata.get('namespace') != cluster.ns or labels.get('app') != app or
                    labels.get('kubeauto.io/component') != 'redis' or
                    labels.get('kubeauto.io/managed-by') != 'kubeauto' or len(owners) != 1 or
                    owners[0].get('uid') != sts['metadata']['uid'] or
                    owners[0].get('kind') != 'StatefulSet' or owners[0].get('name') != app):
                raise ValueError('Cluster rollback refuses a foreign Pod')
            ready = any(c['type'] == 'Ready' and c['status'] == 'True'
                        for c in pod.get('status', {}).get('conditions', []))
            current = next(c['image'] for c in pod['spec']['containers'] if c['name'] == app)
            if not ready and current != image and not metadata.get('deletionTimestamp'):
                deletions.append((metadata['name'], {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                    'gracePeriodSeconds': 30, 'preconditions': {k: metadata[k] for k in ('uid', 'resourceVersion')}}))
    return deletions if templates_ready else None


def reconcile_version(source, cluster, image, *, repair=False):
    command = [str(source.base / '.venv/bin/python'), str(source.base / 'kubecli.py'),
               'setup', 'redis-cluster-gate', '07', '--tags', 'redis']
    if not repair:
        source.run(command, timeout=1400, capture=False)
        return
    # Product setup restores the declaration and performs the normal health gate.
    # Only after its templates are restored may the failed fixture Pods be rebuilt.
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 1400
    rebuilt = False
    try:
        while process.poll() is None:
            if not rebuilt:
                plan = rollback_deletions(cluster, image)
                if plan is not None:
                    for name, options in plan:
                        cluster.k('delete', '--raw', f'/api/v1/namespaces/{cluster.ns}/pods/{name}',
                                  '-f', '-', input_text=json.dumps(options))
                        print('REDIS_CLUSTER_ROLLBACK_MEMBER_REBUILD pod=' + name +
                              ' restored-template=verified uid-precondition=verified', flush=True)
                    rebuilt = True
            if time.monotonic() >= deadline:
                raise ValueError('Cluster rollback product convergence timed out')
            time.sleep(3)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)


def repair_interrupted(source, args, focused, clients, inputs, work):
    fault = source.cluster / 'redis-cluster-upgrade-fault.json'
    state = json.loads(fault.read_text())
    source.identity()
    if not re.fullmatch(r'[0-9a-f]{32}', state['run_id']):
        raise ValueError('Cluster patch repair refuses an invalid run identity')
    config = args.base / 'clusters/redis-cluster-gate/config.yml'
    fixture = json.loads((source.cluster / 'redis-cluster-fixture.json').read_text())
    owner = json.loads((source.cluster / 'redis-lab-owner.json').read_text())
    if (config.parent.is_symlink() or config.is_symlink() or not fixture.get('healthy') or
            fixture['cluster_uid'] != owner['cluster_uid'] or fixture['cr_uid'] != state['cr_uid'] or
            fixture['config_sha256'] != state['original_config_sha256'] or
            json.loads((config.parent / 'redis-fixture-owner.json').read_text()) !=
            {'run_id': fixture['run_id']}):
        raise ValueError('Cluster patch repair refuses a changed fixture boundary')
    before = config.read_bytes()
    original = version_input(before, KubeConstant().v_redis_cluster_image)
    if hashlib.sha256(original).hexdigest() != state['original_config_sha256']:
        raise ValueError('Cluster patch repair refuses changed product input')
    cr = source.get('rediscluster', 'redis-gate-cluster-data')
    metadata = cr['metadata']; labels = metadata.get('labels', {})
    if (metadata['uid'] != state['cr_uid'] or metadata.get('namespace') != source.ns or
            labels.get('kubeauto.io/component') != 'redis' or
            labels.get('kubeauto.io/managed-by') != 'kubeauto'):
        raise ValueError('Cluster patch repair refuses a changed declaration')
    inputs.replace_config(config, [before], original)
    source.run_id = state['run_id']
    source.key = 'kubeauto:redis:focused:' + source.run_id
    source.value = ('verified-' + source.run_id).encode()
    cluster = clients.Cluster(source, focused, work)
    claims = focused.resource_identities(source.get('pvc')['items'])
    image = 'registry.talkschool.cn:5000/brinnatt/redis:' + KubeConstant().v_redis_cluster_image
    reconcile_version(source, cluster, image, repair=True)
    source.forwards.clear(); cluster.health()
    dataset = {'kubeauto:redis:cluster:' + source.run_id + '{soak-' + str(i) + '}':
               ('soak-' + str(i) + '-' + source.run_id).encode() for i in range(16)}
    for key, value in dataset.items():
        if cluster.command(key, 'GET', key) != value or cluster.command(key, 'PTTL', key) <= 0:
            raise ValueError('Cluster patch recovery lost its acknowledged business data')
    if source.client(source.primary(), fresh=True).command('GET', source.key) != source.value:
        raise ValueError('Cluster patch recovery lost its source business data')
    if claims != focused.resource_identities(source.get('pvc')['items']):
        raise ValueError('Cluster patch recovery replaced a customer PVC')
    cluster.keys.update(dataset); cluster.cleanup(); source.cleanup_keys()
    if json.loads(fault.read_text()) != state:
        raise ValueError('Cluster patch recovery refuses a changed fault journal')
    fault.unlink()
    print('REDIS_CLUSTER_UPGRADE_REPAIR_PASS original-input=restored data-and-pvc=retained uuid-keys=absent', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('upgrade-cluster', 'upgrade-cluster-repair'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_cluster_upgrade_focused')
    clients = load(args.base, 'redis-cluster-client-gate.py', 'redis_cluster_upgrade_clients')
    stability = load(args.base, 'redis-stability-gate.py', 'redis_cluster_upgrade_retained')
    inputs = load(args.base, 'redis-capacity-gate.py', 'redis_cluster_upgrade_inputs')
    upgrade = load(args.base, 'redis-upgrade-gate.py', 'redis_cluster_upgrade_aliases')
    with tempfile.TemporaryDirectory(prefix='redis-cluster-upgrade-') as tmp, ExitStack() as stack:
        source = focused.Gate(args, Path(tmp), stack)
        if args.module == 'upgrade-cluster-repair':
            repair_interrupted(source, args, focused, clients, inputs, Path(tmp))
            print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
            print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)
            return
        source.baseline()
        cluster = clients.Cluster(source, focused, Path(tmp))
        cluster.health()
        retained = stability.Stability(source, cluster, focused)
        config = cluster.target / 'config.yml'
        original = config.read_bytes()
        accepted = [original]
        legacy = version_input(original, '8.10.1')
        current_version = KubeConstant().v_redis_cluster_image
        current = version_input(original, current_version)
        fault = source.cluster / 'redis-cluster-upgrade-fault.json'
        if fault.exists():
            raise ValueError('Cluster version gate has an unrecovered mutation')
        evidence = json.loads((args.base / 'tests/evidence/redis-images-20261012.json').read_text())
        origin, target, expected = next(row for row in upgrade.fixture_aliases(evidence) if '/redis:' in row[1])
        probe = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false', 'docker://' + target],
                               capture_output=True, timeout=60)
        if probe.returncode:
            if b'manifest unknown' not in probe.stderr.lower() and b'name unknown' not in probe.stderr.lower():
                raise ValueError('Cluster upgrade fixture cache unavailable')
            source.run(['skopeo', 'copy', '--preserve-digests', '--dest-tls-verify=false',
                        'docker://' + origin, 'docker://' + target], timeout=900)
            probe = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false', 'docker://' + target],
                                   capture_output=True, check=True, timeout=60)
        if 'sha256:' + hashlib.sha256(probe.stdout).hexdigest() != expected:
            raise ValueError('Cluster upgrade fixture alias digest mismatch')
        retained.sample()
        state = {'run_id':source.run_id, 'cr_uid':cluster.journal['cr_uid'],
                 'original_config_sha256':hashlib.sha256(original).hexdigest()}
        with fault.open('x') as out:
            fault.chmod(0o600)
            json.dump(state, out)
            out.flush()
            os.fsync(out.fileno())
        failure = None
        def declare(replacement, version, *, repair=False):
            inputs.replace_config(config, accepted, replacement)
            accepted.append(replacement)
            image = 'registry.talkschool.cn:5000/brinnatt/redis:' + version
            reconcile_version(source, cluster, image, repair=repair)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                if members_ready(cluster.members(), image):
                    break
                print('REDIS_CLUSTER_UPGRADE_HEARTBEAT runtime=' + version, flush=True)
                time.sleep(3)
            else:
                raise ValueError('Cluster patch migration did not roll all nine declared runtime Pods')
            source.forwards.clear()
            retained.verify_retained()
            expected_server = version.split('-')[0]
            for group in cluster.members().values():
                for pod in group:
                    info = dict(l.split(':', 1) for l in cluster.client(pod['metadata']['name'], fresh=True).
                                command('INFO', 'server').decode().splitlines() if ':' in l)
                    if info.get('redis_version') != expected_server:
                        raise ValueError('Cluster patch image differs from actual server version')
        try:
            declare(legacy, '8.10.1')
            print('REDIS_CLUSTER_LEGACY_VERSION_PASS redis=8.10.1 protected-controller=retained', flush=True)
            declare(current, current_version)
            print('REDIS_CLUSTER_VERSION_UPGRADE_PASS redis=8.10.1-to-8.10.2 data-expiry-and-pvc=retained', flush=True)
            declare(legacy, '8.10.1')
            print('REDIS_CLUSTER_SUPPORTED_ROLLBACK_PASS qualification=fixed-topology-patch-pair data=retained', flush=True)
            declare(original, current_version)
            print('REDIS_CLUSTER_VERSION_QUALIFICATION_PASS current-image=restored topology=3+6', flush=True)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                declare(original, current_version, repair=failure is not None)
                cluster.cleanup()
                source.cleanup_keys()
                if json.loads(fault.read_text()) != state:
                    raise ValueError('Cluster patch cleanup refuses a changed journal')
                fault.unlink()
                print('REDIS_CLUSTER_UPGRADE_CLEAN_PASS config=restored uuid-keys=absent journal=removed', flush=True)
            except BaseException:
                if failure is None:
                    raise
                print('REDIS_CLUSTER_UPGRADE_CLEANUP_FAIL first-failure=retained journal=retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=upgrade-cluster', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=upgrade-cluster', flush=True)


if __name__ == '__main__':
    main()
