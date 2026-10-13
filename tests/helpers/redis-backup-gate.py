#!/usr/bin/env python3
"""Product RDB export and independent, authenticated restore into an owned emptyDir."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import time

import yaml


def load_focused(base):
    spec = importlib.util.spec_from_file_location('redis_backup_focused', base / 'tests/helpers/redis-focused-gate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restore_pod(namespace, name, run_id, image):
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {
        'name': name, 'namespace': namespace, 'labels': {
            'kubeauto.io/component': 'redis-restore-test', 'kubeauto.io/managed-by': 'kubeauto',
            'kubeauto.io/test-run': run_id}}, 'spec': {
        'restartPolicy': 'Always', 'automountServiceAccountToken': False,
        'securityContext': {'runAsUser': 999, 'runAsGroup': 999, 'fsGroup': 999},
        'containers': [{'name': 'restore', 'image': image, 'command': ['sh', '-ec',
            'while [ ! -f /work/restore.ready ]; do sleep 1; done; '
            'redis-check-rdb /work/dump.rdb; '
            'exec redis-server --dir /work --dbfilename dump.rdb --appendonly no --save "" '
            '--port 0 --tls-port 6379 --tls-cert-file /tls/tls.crt --tls-key-file /tls/tls.key '
            '--tls-ca-cert-file /tls/ca.crt --tls-auth-clients yes --aclfile /acl/user.acl'],
            'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                          'limits': {'cpu': '1', 'memory': '512Mi'}},
            'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
            'volumeMounts': [{'name': 'work', 'mountPath': '/work'},
                             {'name': 'tls', 'mountPath': '/tls', 'readOnly': True},
                             {'name': 'acl', 'mountPath': '/acl', 'readOnly': True}]}],
        'volumes': [{'name': 'work', 'emptyDir': {'sizeLimit': '256Mi'}},
                    {'name': 'tls', 'secret': {'secretName': 'redis-gate-tls'}},
                    {'name': 'acl', 'secret': {'secretName': 'redis-gate-acl'}}]}}


def verified_artifact(directory, manifest):
    if (manifest.get('mode') != 'sentinel' or len(manifest.get('files', [])) != 1 or
            manifest.get('restore_verified') is not False):
        raise ValueError('backup requires one unmodified Sentinel export manifest')
    record = manifest['files'][0]
    if record['file'] != 'replication.rdb':
        raise ValueError('unexpected backup artifact path')
    artifact = directory / record['file']
    if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size != record['size']:
        raise ValueError('backup artifact identity or size mismatch')
    data = artifact.read_bytes()
    if len(data) < 16 or data[:5] != b'REDIS' or hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('backup artifact header or digest mismatch')
    return artifact


def restart_rdb_target(gate, pod_name, client):
    before = gate.get('pod', pod_name)
    before_run = dict(line.split(':', 1) for line in client.command('INFO', 'server').decode().splitlines()
                      if ':' in line)['run_id']
    before_count = before['status']['containerStatuses'][0]['restartCount']
    try:
        client.command('SHUTDOWN', 'SAVE')
    except (EOFError, OSError):
        pass  # Successful SHUTDOWN closes the protocol stream without a reply.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = gate.get('pod', pod_name)
        if current['metadata']['uid'] != before['metadata']['uid']:
            raise ValueError('RDB restart unexpectedly replaced the target volume owner')
        if current['status']['containerStatuses'][0]['restartCount'] > before_count:
            try:
                restored = gate.client(pod_name, fresh=True)
                after_run = dict(line.split(':', 1) for line in restored.command('INFO', 'server').decode().splitlines()
                                 if ':' in line)['run_id']
                if after_run != before_run:
                    return restored
            except (OSError, EOFError, ValueError):
                pass
        print('REDIS_BACKUP_HEARTBEAT stage=rdb-process-restart', flush=True)
        time.sleep(3)
    raise ValueError('independent RDB target did not restart from its retained snapshot')


def offline_restore_script(text, directory):
    blocks = re.findall(r'```bash\n(.*?)\n```', text, re.DOTALL)
    blocks = [b for b in blocks if b.startswith("bash <<'REDIS_OFFLINE_RDB_VERIFY'\n")]
    original = 'backup_dir=/secure/redis-backups/snapshot-20261012-01'
    if len(blocks) != 1 or blocks[0].count(original) != 1:
        raise ValueError('offline restore requires the unique complete customer main path')
    return blocks[0].replace(original, 'backup_dir=' + shlex.quote(str(directory)), 1)


def cleanup_offline(source):
    name = 'redis-offline-' + source.run_id
    identifier = source.run(['docker', 'container', 'ls', '-aq', '--filter', 'name=^/' + name + '$'])
    if identifier:
        if not re.fullmatch(r'[0-9a-f]{12,64}', identifier):
            raise ValueError('offline restore cleanup refuses ambiguous container identity')
        owner = source.run(['docker', 'inspect', '--format',
                            '{{index .Config.Labels "kubeauto.io/recovery-run"}}', identifier])
        if owner != source.run_id:
            raise ValueError('offline restore cleanup refuses foreign container')
        source.run(['docker', 'rm', '-f', identifier])


def verify_offline_restore(source, directory, probe_key, count):
    script = offline_restore_script((source.base / 'docs/middleware/redis/operations-manual.md').read_text(), directory)
    env = dict(os.environ, RESTORE_RUN_ID=source.run_id, RESTORE_PROBE_KEY=probe_key,
               RESTORE_PROBE_SHA256=hashlib.sha256(source.value).hexdigest(), RESTORE_EXPECTED_KEYS=str(count))
    try:
        result = subprocess.run(['bash'], input=script, text=True, capture_output=True,
                                cwd=source.base, env=env, timeout=240)
        print(result.stdout, flush=True)
        if result.returncode or result.stdout.splitlines().count('REDIS_OFFLINE_RDB_VERIFY_PASS') != 1:
            raise ValueError('customer offline RDB recovery path failed; rc=' + str(result.returncode))
    finally:
        cleanup_offline(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('backup',))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load_focused(args.base)
    with tempfile.TemporaryDirectory(prefix='redis-backup-') as temporary, ExitStack() as stack:
        gate = focused.Gate(args, Path(temporary), stack)
        gate.baseline()
        config_path = gate.cluster / 'config.yml'
        original_config = config_path.read_bytes()
        request = dict(gate.config, redis_snapshot_requested=True,
                       redis_snapshot_directory=str(gate.work / 'snapshot'))
        request_bytes = yaml.safe_dump(request, sort_keys=False).encode()
        pod_name = 'redis-restore-' + gate.run_id[:12]
        keys = [gate.key + ':' + kind for kind in ('string', 'hash', 'list', 'set', 'zset')]
        source_claims = focused.resource_identities(gate.get('pvc')['items'])
        pod_intended = False
        failure = None
        try:
            primary = gate.primary()
            client = gate.client(primary)
            for command in (('SET', keys[0], gate.value), ('HSET', keys[1], 'field', gate.value),
                            ('RPUSH', keys[2], gate.value), ('SADD', keys[3], gate.value),
                            ('ZADD', keys[4], 1, gate.value)):
                client.command(*command)
            for key in keys:
                if client.command('EXPIRE', key, 3600) != 1:
                    raise ValueError('backup fixture TTL failed')
            expires_at = time.time() + 3600
            if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                raise ValueError('backup source has not persisted on all members')
            count = client.command('DBSIZE')
            config_path.write_bytes(request_bytes)
            gate.run([str(gate.base / '.venv/bin/python'), str(gate.base / 'kubecli.py'),
                      'setup', 'redis-gate', '07', '--tags', 'redis-snapshot'], timeout=2000, capture=False)
            directory = gate.work / 'snapshot'
            manifest = json.loads((directory / 'manifest.json').read_text())
            artifact = verified_artifact(directory, manifest)
            if gate.primary() != primary:
                raise ValueError('backup source primary changed during the write pause')
            pod_intended = True
            gate.k('create', '-f', '-', input_text=json.dumps(restore_pod(
                gate.ns, pod_name, gate.run_id, 'registry.talkschool.cn:5000/brinnatt/redis:' + gate.config['redis_ver'])))
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                pod = gate.get('pod', pod_name)
                if pod['status'].get('phase') == 'Running':
                    break
                if pod['status'].get('phase') in ('Failed', 'Succeeded'):
                    raise ValueError('restore target exited before accepting its artifact')
                print('REDIS_BACKUP_HEARTBEAT stage=target-start', flush=True)
                time.sleep(5)
            else:
                raise ValueError('restore target startup timeout')
            # File bytes travel on stdin; no source volume is attached to target.
            with artifact.open('rb') as stream:
                result = subprocess.run([*gate.kubectl, '-n', gate.ns, 'exec', '-i', pod_name, '--',
                    'sh', '-ec', 'cat > /work/dump.rdb.part; mv /work/dump.rdb.part /work/dump.rdb'],
                    stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
            if result.returncode:
                raise ValueError('restore artifact stream failed')
            digest = gate.k('exec', pod_name, '-n', gate.ns, '--', 'sha256sum', '/work/dump.rdb').split()[0]
            if digest != manifest['files'][0]['sha256']:
                raise ValueError('restore target artifact digest mismatch')
            gate.k('exec', pod_name, '-n', gate.ns, '--', 'redis-check-rdb', '/work/dump.rdb')
            gate.k('exec', pod_name, '-n', gate.ns, '--', 'touch', '/work/restore.ready')
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    restored = gate.client(pod_name, fresh=True)
                    if restored.command('PING') == b'PONG':
                        break
                except (OSError, EOFError, ValueError):
                    pass
                print('REDIS_BACKUP_HEARTBEAT stage=rdb-load', flush=True)
                time.sleep(3)
            else:
                raise ValueError('restore Redis readiness timeout')
            if restored.command('DBSIZE') != count:
                raise ValueError('restored key count mismatch')
            reads = [('GET', keys[0]), ('HGET', keys[1], 'field'), ('LINDEX', keys[2], 0),
                     ('SMEMBERS', keys[3]), ('ZRANGE', keys[4], 0, -1)]
            for key, kind, read in zip(keys, ('string', 'hash', 'list', 'set', 'zset'), reads):
                value = restored.command(*read)
                if isinstance(value, list):
                    value = value[0] if len(value) == 1 else None
                ttl = restored.command('TTL', key)
                if (value != gate.value or restored.command('TYPE', key) != kind.encode() or
                        ttl <= 0 or abs(ttl - (expires_at - time.time())) > 5):
                    raise ValueError('restored type, digest or absolute expiry mismatch')
            if (restored.command('SET', gate.key, gate.value, 'EX', 600) != b'OK' or
                    restored.command('GET', gate.key) != gate.value):
                raise ValueError('restore target does not accept new business writes')
            restored = restart_rdb_target(gate, pod_name, restored)
            if (restored.command('DBSIZE') != count + 1 or restored.command('GET', gate.key) != gate.value or
                    restored.command('TTL', gate.key) <= 0):
                raise ValueError('RDB process restart lost new business data or key count')
            for key, read in zip(keys, reads):
                value = restored.command(*read)
                if isinstance(value, list):
                    value = value[0] if len(value) == 1 else None
                ttl = restored.command('TTL', key)
                if value != gate.value or ttl <= 0 or abs(ttl - (expires_at - time.time())) > 5:
                    raise ValueError('RDB process restart lost retained business digest or absolute expiry')
            print('REDIS_RDB_RESTART_PASS run-id=changed volume-owner=retained count=verified '
                  'digest=verified ttl=verified new-write=retained', flush=True)
            verify_offline_restore(gate, directory, keys[0], count)
            gate.health()
            if focused.resource_identities(gate.get('pvc')['items']) != source_claims:
                raise ValueError('backup modified source PVC identity')
            print('REDIS_INDEPENDENT_RDB_RESTORE_PASS product-export=verified crc=verified '
                  'sha256=' + digest + ' types=5 count=verified ttl=verified new-write=verified source-pvcs=retained', flush=True)
        except BaseException as exc:
            failure = exc
            try:
                gate.fault_diagnostics()
                if pod_intended:
                    print(gate.k('logs', pod_name, '-n', gate.ns, '--tail=60'), flush=True)
            except Exception:
                print('REDIS_BACKUP_DIAGNOSTICS_FAIL first failure retained', flush=True)
            raise
        finally:
            cleanup_errors = []
            def cleanup(action):
                try:
                    action()
                except Exception as exc:
                    cleanup_errors.append(exc)
            def restore_config():
                if config_path.read_bytes() not in (original_config, request_bytes):
                    raise ValueError('backup refuses concurrent config mutation')
                config_path.write_bytes(original_config)
            def remove_target():
                docs = json.loads(gate.k('get', 'pod', pod_name, '-n', gate.ns, '--ignore-not-found', '-o', 'json') or '{}')
                if not docs:
                    return
                labels = docs['metadata'].get('labels', {})
                if any(labels.get(k) != v for k, v in {
                    'kubeauto.io/component': 'redis-restore-test', 'kubeauto.io/managed-by': 'kubeauto',
                    'kubeauto.io/test-run': gate.run_id}.items()):
                    raise ValueError('backup refuses foreign restore target')
                gate.k('delete', 'pod', pod_name, '-n', gate.ns, '--wait=true', '--timeout=120s', timeout=140)
            cleanup(restore_config)
            if pod_intended:
                cleanup(remove_target)
            def remove_keys():
                client = gate.client(gate.primary(), fresh=True)
                client.command('DEL', *keys)
                if client.command('EXISTS', *keys) != 0:
                    raise ValueError('backup source fixture keys remain')
            cleanup(remove_keys)
            if cleanup_errors:
                if failure is None:
                    raise cleanup_errors[0]
                print('REDIS_BACKUP_CLEANUP_FAIL first failure retained', flush=True)
            else:
                print('REDIS_BACKUP_SCOPED_CLEAN_PASS config=restored target=absent source-keys=absent', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=backup', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=backup', flush=True)


if __name__ == '__main__':
    main()
