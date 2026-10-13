#!/usr/bin/env python3
"""Product memory watermark, real replica lag, bounded PVC pressure and online expansion."""
import argparse
from contextlib import ExitStack
import importlib.util
import os
import ipaddress
from pathlib import Path
import re
import signal
import tempfile
import time

import yaml

PRESSURE_REMOVE = (
    'test ! -L "$1" && test ! -L "$1.owner" || exit 1; '
    'if test ! -e "$1" && test ! -e "$1.owner"; then exit 0; fi; '
    'test -f "$1" && test ! -L "$1" && test -f "$1.owner" && test ! -L "$1.owner" || exit 1; '
    'test "$(cat "$1.owner")" = "$2" || exit 1; rm -- "$1" "$1.owner"'
)


def memory_converged(gate, configuration):
    """Prove the lab's requested memory setting is in every completed member."""
    if configuration['redis_memory_limit'] != '512Mi':
        raise ValueError('memory convergence requires the pinned 512Mi lab limit')
    percent = int(configuration['redis_maxmemory_percent'])
    if not 0 < percent <= 100:
        raise ValueError('invalid memory convergence percentage')
    expected = str(512 * 1024 * 1024 * percent // 100)
    def target_container(containers):
        container = next((c for c in containers if c['name'] == gate.name), {})
        return (container.get('resources', {}).get('limits', {}).get('memory') == '512Mi' and
                [e.get('value') for e in container.get('env', [])
                 if e['name'] == 'REDIS_MAX_MEMORY'] == [expected])
    sts = gate.get('statefulset', gate.name)
    metadata = sts['metadata']; status = sts.get('status', {})
    if (metadata.get('namespace') != gate.ns or metadata.get('name') != gate.name or
            sts['spec'].get('replicas') != 3 or
            not target_container(sts['spec']['template']['spec']['containers']) or
            status.get('observedGeneration', 0) < metadata['generation'] or
            not status.get('updateRevision') or status.get('currentRevision') != status['updateRevision'] or
            status.get('readyReplicas') != 3 or status.get('updatedReplicas') != 3):
        return False
    pods = gate.members()['replication']
    if {p['metadata']['name'] for p in pods} != {f'{gate.name}-{i}' for i in range(3)} or len(pods) != 3:
        return False
    for pod in pods:
        meta = pod['metadata']
        if (meta.get('deletionTimestamp') or meta.get('namespace') != gate.ns or
                meta.get('labels', {}).get('controller-revision-hash') != status['updateRevision'] or
                not any(r.get('kind') == 'StatefulSet' and r.get('name') == gate.name and
                        r.get('uid') == metadata['uid'] for r in meta.get('ownerReferences', [])) or
                pod.get('status', {}).get('phase') != 'Running' or
                not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                        for c in pod.get('status', {}).get('conditions', [])) or
                not target_container(pod['spec']['containers'])):
            return False
    return all(gate.client(p['metadata']['name'], fresh=True).command('CONFIG', 'GET', 'maxmemory') ==
               [b'maxmemory', expected.encode()] for p in pods)


def wait_memory_convergence(gate, configuration, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if memory_converged(gate, configuration):
            gate.forwards.clear()
            gate.health()
            print('REDIS_CAPACITY_MEMORY_CONVERGENCE_PASS percent=' +
                  str(configuration['redis_maxmemory_percent']) + ' template-and-runtime=verified', flush=True)
            return
        print('REDIS_CAPACITY_HEARTBEAT stage=memory-configuration-rollout', flush=True)
        time.sleep(2)
    raise ValueError('capacity memory configuration did not finish its actual rollout')


def replace_config(path, expected, replacement):
    """Preserve permissions and refuse a concurrently changed product input."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.redis-capacity-', delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(replacement)
            out.flush()
            os.fsync(out.fileno())
            temporary.chmod(path.stat().st_mode & 0o777)
            if path.read_bytes() not in expected:
                raise ValueError('capacity refuses a concurrent product config mutation')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def resolver_addresses(text, expected):
    addresses = tuple(line.split()[1] for line in text.splitlines() if line.startswith('nameserver '))
    if not addresses or len(set(addresses)) != len(addresses) or set(addresses) != {expected}:
        raise ValueError('capacity refuses an unexplained DNS resolver path')
    for address in addresses:
        ipaddress.IPv4Address(address)
    return addresses


def sever_replication(client):
    # Redis 8.10.2 CLIENT KILL classifies the upstream link as master,
    # independently of normal/pubsub/downstream replica connections.
    count = client.command('CLIENT', 'KILL', 'TYPE', 'master', 'SKIPME', 'yes')
    if not isinstance(count, int) or count < 1:
        raise ValueError('replica fault did not terminate its real upstream master connection')
    print('REDIS_CAPACITY_REPLICATION_RESET_PASS upstream-connections=' + str(count), flush=True)


def growth_target(current, namespace):
    labels = current['metadata'].get('labels', {})
    spec = current['spec']
    size = re.fullmatch(r'([2-4])Gi', spec['resources']['requests']['storage'])
    if (current['metadata'].get('namespace') != namespace or
            labels.get('kubeauto.io/component') != 'redis' or
            labels.get('kubeauto.io/managed-by') != 'kubeauto' or
            spec.get('storageClassName') != 'redis-longhorn' or not size):
        raise ValueError('capacity expansion refuses changed ownership, class or lab size boundary')
    return str(int(size[1]) + 1) + 'Gi'


def filesystem(gate, pod):
    container = gate.get('pod', pod)['spec']['containers'][0]['name']
    row = gate.k('exec', pod, '-n', gate.ns, '-c', container, '--', 'df', '-Pk', '/data').splitlines()[-1].split()
    return {'total': int(row[1]) * 1024, 'used': int(row[2]) * 1024, 'available': int(row[3]) * 1024}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('capacity', 'capacity-lag'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    spec = importlib.util.spec_from_file_location('redis_capacity_focused', args.base / 'tests/helpers/redis-focused-gate.py')
    focused = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(focused)
    with tempfile.TemporaryDirectory(prefix='redis-capacity-') as temporary, ExitStack() as stack:
        gate = focused.Gate(args, Path(temporary), stack)
        gate.baseline()
        config_path = gate.cluster / 'config.yml'
        original = config_path.read_bytes()
        request = yaml.safe_dump(dict(gate.config, redis_maxmemory_percent=50), sort_keys=False).encode()
        accepted_inputs = [original, request]
        retained_config = original
        keys = [gate.key + ':memory:' + str(i) for i in range(192)]
        lag_key = gate.key + ':lag'
        disk_path = '/data/kubeauto-redis-pressure-' + gate.run_id + '.bin'
        disk_pod = None
        configured = False
        failure = None
        claims_before = focused.resource_identities(gate.get('pvc')['items'])

        def product_setup():
            gate.run([str(gate.base / '.venv/bin/python'), str(gate.base / 'kubecli.py'),
                      'setup', 'redis-gate', '07', '--tags', 'redis'], timeout=1000, capture=False)
            gate.forwards.clear()
            gate.health()
            wait_memory_convergence(gate, yaml.safe_load(config_path.read_bytes()))

        try:
            _, baseline = gate.seeded_primary()
            baseline.command('EXPIRE', gate.key, 7200)
            if args.module == 'capacity':
                configured = True
                replace_config(config_path, [original], request)
                product_setup()
                primary = gate.primary()
                client = gate.client(primary, fresh=True)
                if client.command('GET', gate.key) != gate.value:
                    raise ValueError('product watermark change lost retained business data')
                if client.command('CONFIG', 'GET', 'maxmemory') != [b'maxmemory', b'268435456']:
                    raise ValueError('product 50 percent watermark does not match the 512Mi lab limit')
                if client.command('CONFIG', 'GET', 'maxmemory-policy') != [b'maxmemory-policy', b'noeviction']:
                    raise ValueError('memory pressure requires the product noeviction policy')
                filled = 0
                for key in keys:
                    try:
                        client.command('SET', key, b'm' * (2 * 1024 * 1024), 'EX', 1800)
                        filled += 1
                    except focused.RedisReplyError as error:
                        if error.code != 'OOM':
                            raise
                        if client.command('EXISTS', key) != 0:
                            raise ValueError('OOM write unexpectedly created its key')
                        break
                else:
                    raise ValueError('bounded product memory pressure did not reach OOM rejection')
                if client.command('GET', gate.key) != gate.value:
                    raise ValueError('noeviction lost an existing business key')
                client.command('DEL', *keys)
                if (client.command('SET', gate.key, gate.value, 'KEEPTTL') != b'OK' or
                        client.command('WAITAOF', 1, 2, 5000) != [1, 2]):
                    raise ValueError('memory reclamation did not restore durable writes')
                print(f'REDIS_MEMORY_WATERMARK_PASS product-percent=50 filled-keys={filled} '
                      'oom=denied existing-key=retained post-reclaim-write=verified', flush=True)

            else:
                primary = gate.primary()
                client = gate.client(primary, fresh=True)
            client.command('SET', lag_key, 'before', 'EX', 1200)
            if client.command('WAIT', 2, 5000) != 2:
                raise ValueError('replica lag baseline is not fully replicated')
            replica = next(p['metadata']['name'] for p in gate.members()['replication']
                           if p['metadata']['name'] != primary)
            # Redis reconnect resolves its announced hostname synchronously.
            # Isolate data traffic while preserving the fixture's DNS path.
            container = gate.get('pod', replica)['spec']['containers'][0]['name']
            resolvers = resolver_addresses(gate.k('exec', replica, '-n', gate.ns, '-c', container, '--',
                'cat', '/etc/resolv.conf'), gate.config['LOCAL_DNS_CACHE'])
            if not gate.config['ENABLE_LOCAL_DNS_CACHE']:
                raise ValueError('capacity fixture requires its verified NodeLocal DNS configuration')
            with gate.isolated([replica], dns_addresses=resolvers):
                # NetworkPolicy need not terminate an already established TCP link.
                time.sleep(3)
                stale = gate.disconnect_existing(replica)
                sever_replication(stale)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    info = dict(line.split(':', 1) for line in stale.command('INFO', 'replication').decode().splitlines()
                                if ':' in line)
                    if info.get('master_link_status') == 'down':
                        break
                    print('REDIS_CAPACITY_HEARTBEAT stage=replica-disconnect', flush=True)
                    time.sleep(2)
                else:
                    raise ValueError('lag fixture did not sever the selected replication link')
                client.command('SET', lag_key, 'after', 'KEEPTTL')
                if client.command('WAIT', 1, 5000) != 1 or stale.command('GET', lag_key) != b'before':
                    raise ValueError('lag scenario lacks one healthy acknowledgement or a real stale replica')
            client = gate.client(gate.primary(), fresh=True)
            if (client.command('WAIT', 2, 5000) != 2 or
                    gate.client(replica, fresh=True).command('GET', lag_key) != b'after'):
                raise ValueError('replica did not catch up after the bounded lag fault')
            print('REDIS_REPLICATION_LAG_PASS isolated-replica=stale healthy-ack=1 recovered-acks=2 digest=verified', flush=True)

            if args.module == 'capacity':
                pressure_target = gate.primary()
                disk_before = filesystem(gate, pressure_target)
                if disk_before['available'] < 384 * 1024 * 1024:
                    raise ValueError('bounded disk pressure would violate the 256Mi reserve')
                container = gate.get('pod', pressure_target)['spec']['containers'][0]['name']
                gate.k('exec', pressure_target, '-n', gate.ns, '-c', container, '--', 'sh', '-ec',
                       'test ! -e "$1" && test ! -e "$1.owner"', 'redis-owned-pressure', disk_path)
                disk_pod = pressure_target
                gate.k('exec', disk_pod, '-n', gate.ns, '-c', container, '--', 'sh', '-ec',
                       'set -C; : > "$1"; printf "%s" "$2" > "$1.owner"; '
                       'dd if=/dev/zero of="$1" bs=1M count=128 conv=fsync',
                       'redis-owned-pressure', disk_path, gate.run_id, timeout=180)
                disk_after = filesystem(gate, disk_pod)
                if disk_after['used'] - disk_before['used'] < 120 * 1024 * 1024:
                    raise ValueError('disk pressure did not consume the intended bounded PVC space')
                client = gate.client(gate.primary(), fresh=True)
                if client.command('GET', gate.key) != gate.value or client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                    raise ValueError('bounded disk pressure lost retained durable business data')
                gate.k('exec', disk_pod, '-n', gate.ns, '-c', container, '--', 'sh', '-ec',
                       PRESSURE_REMOVE,
                       'redis-owned-pressure', disk_path, gate.run_id)
                disk_pod = None
                print('REDIS_BOUNDED_DISK_PRESSURE_PASS bytes=134217728 reserve-min=268435456 fsync=verified digest=retained file=removed', flush=True)

                pods_before = focused.resource_identities(gate.get('pods')['items'])
                claim_objects = gate.get('pvc')['items']
                targets = {growth_target(claim, gate.ns) for claim in claim_objects}
                if len(claim_objects) != 3 or len(targets) != 1:
                    raise ValueError('product expansion requires three consistent owned source claims')
                target = targets.pop()
                filesystems = {}
                for claim in claim_objects:
                    name = claim['metadata']['name']
                    pod = next(p['metadata']['name'] for p in gate.members()['replication']
                               if any(v.get('persistentVolumeClaim', {}).get('claimName') == name
                                      for v in p['spec']['volumes']))
                    filesystems[name] = (pod, filesystem(gate, pod))
                # v0.27.0 HandlePVCResizing grows existing claims through the CR.
                # Keep the accepted grow-only size in this runner-owned baseline;
                # restoring the memory setting must not request a PVC shrink.
                request = yaml.safe_dump(dict(gate.config, redis_maxmemory_percent=50, redis_pvc_size=target), sort_keys=False).encode()
                accepted_inputs.append(request)
                retained_config = yaml.safe_dump(dict(gate.config, redis_pvc_size=target), sort_keys=False).encode()
                replace_config(config_path, accepted_inputs[:-1], request)
                product_setup()
                for claim in claim_objects:
                    name = claim['metadata']['name']
                    pod, fs_before = filesystems[name]
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        current = gate.get('pvc', name)
                        fs = filesystem(gate, pod)
                        if (current['status'].get('capacity', {}).get('storage') == target and
                                not current['status'].get('conditions') and fs['total'] > fs_before['total'] and
                                fs['total'] >= int(target[:-2]) * 1024**3 * .9):
                            break
                        print('REDIS_CAPACITY_HEARTBEAT stage=pvc-expand claim=' + name + ' target=' + target, flush=True)
                        time.sleep(5)
                    else:
                        raise ValueError('PVC/file-system online expansion did not complete')
                    print('REDIS_PVC_MEMBER_EXPANSION_PASS claim=' + name + ' target=' + target + ' filesystem=verified', flush=True)
                product_setup()
                if (focused.resource_identities(gate.get('pvc')['items']) != claims_before or
                        focused.resource_identities(gate.get('pods')['items']) != pods_before):
                    raise ValueError('online PVC expansion or second product execution replaced business resources')
                client = gate.client(gate.primary(), fresh=True)
                if client.command('GET', gate.key) != gate.value or client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                    raise ValueError('online expansion lost durable business data')
                print('REDIS_PVC_EXPANSION_PASS members=3 pvc-and-pod-uids=retained product-reconcile=verified digest=retained', flush=True)
        except BaseException as exc:
            failure = exc
            try:
                gate.fault_diagnostics()
            except Exception:
                print('REDIS_CAPACITY_DIAGNOSTICS_FAIL first failure retained', flush=True)
            raise
        finally:
            errors = []
            def cleanup(action):
                try:
                    action()
                except Exception as exc:
                    errors.append(exc)
            def remove_keys():
                client = gate.client(gate.primary(), fresh=True)
                client.command('DEL', *keys, lag_key)
                if client.command('EXISTS', *keys, lag_key) != 0:
                    raise ValueError('capacity owned keys remain')
            cleanup(remove_keys)
            if disk_pod:
                def remove_pressure():
                    container = gate.get('pod', disk_pod)['spec']['containers'][0]['name']
                    gate.k('exec', disk_pod, '-n', gate.ns, '-c', container, '--', 'sh', '-ec',
                           PRESSURE_REMOVE,
                           'redis-owned-pressure', disk_path, gate.run_id)
                cleanup(remove_pressure)
            if configured:
                def restore_configuration():
                    replace_config(config_path, accepted_inputs, retained_config)
                    product_setup()
                    print('REDIS_CAPACITY_CONFIGURATION_RESTORED_PASS template-and-runtime=verified '
                          'no-pending-rollout', flush=True)
                cleanup(restore_configuration)
            cleanup(gate.cleanup_keys)
            if errors:
                if failure is None:
                    raise errors[0]
                print('REDIS_CAPACITY_CLEANUP_FAIL first failure retained', flush=True)
            else:
                print('REDIS_CAPACITY_SCOPED_CLEAN_PASS keys=absent fault=absent pressure-file=absent '
                      'original-memory-config=restored grown-product-pvcs=retained', flush=True)
    if args.module == 'capacity-lag':
        print('REDIS_CAPACITY_LAG_PASS stale=verified catch-up=verified', flush=True)
    else:
        print('REDIS_CAPACITY_PASS memory-watermark=verified replica-lag=recovered bounded-disk=verified '
              'product-pvc-expansion=verified data=retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
