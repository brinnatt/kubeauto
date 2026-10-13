#!/usr/bin/env python3
"""Owned credential rotation with staged CA trust and maintenance-window password changes."""
import argparse
import base64
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import signal
import ssl
import subprocess
import tempfile


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def encoded(values):
    return {key: base64.b64encode(value).decode() for key, value in values.items()}


def verify_rotation_consumers(gate, module):
    """A Sentinel-only roll cannot refresh another declaration's credentials."""
    changed = ({'redis-gate-auth', 'redis-gate-acl'} if module == 'password-rotation'
               else {'redis-gate-tls'})
    owned = {('RedisReplication', gate.name), ('RedisSentinel', gate.name + '-sentinel')}
    for obj in gate.get('redis,rediscluster,redisreplication,redissentinel')['items']:
        if (obj['kind'], obj['metadata']['name']) in owned:
            continue
        spec = obj['spec']
        references = {
            spec.get('kubernetesConfig', {}).get('redisSecret', {}).get('name'),
            spec.get('acl', {}).get('secret', {}).get('secretName'),
            spec.get('TLS', {}).get('secret', {}).get('secretName'),
        }
        if changed & references:
            raise ValueError('Sentinel rotation refuses credentials shared by another Redis declaration')
    print('REDIS_ROTATION_CONSUMER_PREFLIGHT_PASS shared-consumers=none', flush=True)


def verify_tls_material(work, old, new):
    """Disprove issuer ambiguity using the control's actual OpenSSL before mutation."""
    bundle = old['ca.crt'] + b'\n' + new['ca.crt']
    for filename, data in (('verify-bundle.pem', bundle), ('verify-new-ca.pem', new['ca.crt']),
                           ('verify-old-leaf.pem', old['tls.crt']), ('verify-new-leaf.pem', new['tls.crt'])):
        (work / filename).write_bytes(data)
        (work / filename).chmod(0o600)
    for ca, leaf, accepted in (('verify-bundle.pem', 'verify-old-leaf.pem', True),
                              ('verify-bundle.pem', 'verify-new-leaf.pem', True),
                              ('verify-new-ca.pem', 'verify-new-leaf.pem', True),
                              ('verify-new-ca.pem', 'verify-old-leaf.pem', False)):
        result = subprocess.run(['openssl', 'verify', '-CAfile', str(work / ca), str(work / leaf)],
                                capture_output=True, timeout=10)
        if (result.returncode == 0) != accepted:
            raise ValueError('TLS rotation issuer preflight failed before Secret mutation')
    print('REDIS_TLS_ISSUER_PREFLIGHT_PASS old-and-new-leaves=trusted retired-ca=denied', flush=True)
    return bundle


def replace_document(current, original, expected, values):
    metadata = current['metadata']
    labels = metadata.get('labels', {})
    if (metadata['uid'] != original['metadata']['uid'] or
            metadata['name'] != original['metadata']['name'] or
            metadata['namespace'] != original['metadata']['namespace'] or
            labels.get('kubeauto.io/component') != 'redis-test' or
            labels.get('kubeauto.io/managed-by') != 'kubeauto' or current['data'] != expected):
        raise ValueError('rotation refuses changed Secret identity, ownership or concurrent data')
    return {'apiVersion': 'v1', 'kind': 'Secret', 'type': current['type'], 'data': encoded(values),
            'metadata': {key: metadata[key] for key in ('name', 'namespace', 'labels', 'resourceVersion')}}


class SecretRotation:
    """CAS writes, including a write accepted before a transport timeout."""

    def __init__(self, gate, originals):
        self.gate, self.originals = gate, originals
        self.expected = {name: doc['data'].copy() for name, doc in originals.items()}
        self.mutated = []

    def replace(self, name, values):
        current = self.gate.get('secret', name)
        document = replace_document(current, self.originals[name], self.expected[name], values)
        # Record intent before I/O. A failed readback leaves the scope marked
        # dirty, and rollback refuses an unproven state rather than overwriting.
        if name not in self.mutated:
            self.mutated.append(name)
        try:
            self.gate.k('replace', '-f', '-', input_text=json.dumps(document))
        except Exception:
            try:
                observed = self.gate.get('secret', name)
                replace_document(observed, self.originals[name], document['data'], values)
            except Exception:
                print('REDIS_ROTATION_WRITE_UNCERTAIN readback=unproven', flush=True)
            else:
                self.expected[name] = document['data']
                print('REDIS_ROTATION_WRITE_READBACK accepted=verified', flush=True)
            raise
        self.expected[name] = document['data']

    def restore(self):
        for name in reversed(self.mutated):
            self.replace(name, {k: base64.b64decode(v) for k, v in self.originals[name]['data'].items()})


def recover(gate, rotation, roll):
    """Diagnostics and rollback must never hide the owning first failure."""
    print('REDIS_ROTATION_FIRST_FAILURE preserved-before-rollback', flush=True)
    try:
        gate.fault_diagnostics()
    except Exception:
        print('REDIS_ROTATION_DIAGNOSTICS_FAIL original failure retained', flush=True)
    if rotation.mutated:
        try:
            rotation.restore()
            roll(repair=True)
            print('REDIS_ROTATION_ROLLBACK_PASS originals=restored business=verified', flush=True)
        except Exception:
            print('REDIS_ROTATION_ROLLBACK_FAIL original failure retained', flush=True)


def broken_member_deletions(gate):
    """Validate the whole scope before deleting a single failed OrderedReady Pod."""
    result = []
    expected = {gate.name: 'replication', gate.name + '-sentinel-sentinel': 'sentinel'}
    members = gate.members()
    for name, role in expected.items():
        statefulset = gate.get('statefulset', name)
        if len(members[role]) != 3:
            raise ValueError('rotation recovery requires the exact six-member fixture')
        for pod in members[role]:
            metadata = pod['metadata']
            labels = metadata.get('labels', {})
            owner = [o for o in metadata.get('ownerReferences', []) if o.get('controller')]
            if (metadata.get('namespace') != gate.ns or
                    labels.get('kubeauto.io/component') != 'redis' or
                    labels.get('kubeauto.io/managed-by') != 'kubeauto' or
                    len(owner) != 1 or owner[0].get('kind') != 'StatefulSet' or
                    owner[0].get('name') != name or owner[0].get('uid') != statefulset['metadata']['uid']):
                raise ValueError('rotation recovery refuses foreign member identity or ownership')
            if any(c.get('type') == 'Ready' and c.get('status') == 'True'
                   for c in pod.get('status', {}).get('conditions', [])):
                continue
            options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'gracePeriodSeconds': 30,
                       'preconditions': {k: metadata[k] for k in ('uid', 'resourceVersion')}}
            result.append((metadata['name'], options))
    return result


def rebuild_broken_members(gate):
    gate.identity()
    for name, options in broken_member_deletions(gate):
        gate.k('delete', '--raw', f'/api/v1/namespaces/{gate.ns}/pods/{name}', '-f', '-',
               input_text=json.dumps(options))
        print('REDIS_ROTATION_BROKEN_MEMBER_REBUILD requested=' + name + ' uid-precondition=verified', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('tls-rotation', 'password-rotation', 'rotation-repair'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load_module('redis_focused_rotation', args.base / 'tests/helpers/redis-focused-gate.py')
    fixtures = load_module('redis_rotation_fixtures', args.base / 'tests/helpers/redis-lab-config.py')
    if args.module == 'rotation-repair':
        with tempfile.TemporaryDirectory(prefix='redis-rotation-repair-') as temporary, ExitStack() as stack:
            gate = focused.Gate(args, Path(temporary), stack)
            claims = focused.resource_identities(gate.get('pvc')['items'])
            # Prior rollback restored the Secret; verify its actual chain before
            # rebuilding the exact remaining failed members. Never invent keys.
            result = subprocess.run(['openssl', 'verify', '-CAfile', str(gate.work / 'ca.crt'),
                                     str(gate.work / 'tls.crt')], capture_output=True, timeout=10)
            if result.returncode:
                raise ValueError('rotation repair requires a valid restored credential chain')
            rebuild_broken_members(gate)
            gate.forwards.clear()
            gate.health()
            gate.repair_sentinel()
            if focused.resource_identities(gate.get('pvc')['items']) != claims:
                raise ValueError('rotation repair replaced a data PVC')
            print('REDIS_ROTATION_REPAIR_PASS business=healthy pvc-uids=retained owned-keys=removed', flush=True)
        print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=rotation-repair', flush=True)
        print('REDIS_FOCUSED_MODULE_PASS module=rotation-repair', flush=True)
        return
    with tempfile.TemporaryDirectory(prefix='redis-rotation-') as temporary, ExitStack() as stack:
        gate = focused.Gate(args, Path(temporary), stack)
        gate.baseline()
        verify_rotation_consumers(gate, args.module)
        originals = {name: gate.get('secret', name) for name in ('redis-gate-auth', 'redis-gate-acl', 'redis-gate-tls')}
        rotation = SecretRotation(gate, originals)
        claims = focused.resource_identities(gate.get('pvc')['items'])
        failure = None

        def replace(name, values):
            rotation.replace(name, values)

        def refresh():
            gate.passwords = gate.secret('redis-gate-auth')
            tls = gate.secret('redis-gate-tls')
            for key in ('ca.crt', 'tls.crt', 'tls.key'):
                (gate.work / key).write_bytes(tls[key])
                (gate.work / key).chmod(0o600)
            context = ssl.create_default_context(cafile=str(gate.work / 'ca.crt'))
            context.load_cert_chain(str(gate.work / 'tls.crt'), str(gate.work / 'tls.key'))
            gate.context = context
            gate.forwards.clear()

        def roll(*, repair=False):
            # Each StatefulSet rolls one member at a time. Password changes
            # use a declared write pause; no zero-downtime claim is made.
            if repair:
                rebuild_broken_members(gate)
            for name in (gate.name, gate.name + '-sentinel-sentinel'):
                gate.k('-n', gate.ns, 'rollout', 'restart', 'statefulset/' + name)
                gate.k('-n', gate.ns, 'rollout', 'status', 'statefulset/' + name,
                       '--timeout=600s', timeout=620, capture=False)
            refresh()
            gate.health()
            client = gate.client(gate.primary(), fresh=True)
            if client.command('GET', gate.key) != gate.value or client.command('TTL', gate.key) <= 0:
                raise ValueError('rotation lost the retained business digest or TTL')
            if focused.resource_identities(gate.get('pvc')['items']) != claims:
                raise ValueError('rotation replaced a data PVC')

        try:
            _, client = gate.seeded_primary()
            # Bound the retained fixture independently of the shorter fault
            # scenarios: three CA phases can each require six member restarts.
            if client.command('EXPIRE', gate.key, 7200) != 1:
                raise ValueError('rotation fixture TTL could not be established')
            if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                raise ValueError('rotation baseline is not durably replicated')
            new = {doc['metadata']['name']: {k: v.encode() for k, v in doc['stringData'].items()}
                   for doc in fixtures.credentials()}
            if args.module == 'tls-rotation':
                old = {k: base64.b64decode(v) for k, v in originals['redis-gate-tls']['data'].items()}
                old_context = gate.context
                bundle = verify_tls_material(gate.work, old, new['redis-gate-tls'])
                replace('redis-gate-tls', {**old, 'ca.crt': bundle})
                roll()
                print('REDIS_TLS_ROTATION_PHASE_PASS phase=trust-bundle', flush=True)
                replace('redis-gate-tls', {**new['redis-gate-tls'], 'ca.crt': bundle})
                roll()
                print('REDIS_TLS_ROTATION_PHASE_PASS phase=new-leaf', flush=True)
                replace('redis-gate-tls', new['redis-gate-tls'])
                roll()
                primary = gate.primary()
                pod = gate.get('pod', primary)
                hostname = f"{pod['spec']['hostname']}.{pod['spec']['subdomain']}.{gate.ns}.svc.cluster.local"
                if gate.client(primary, server_name=hostname, fresh=True).command('GET', gate.key) != gate.value:
                    raise ValueError('new leaf SAN does not cover the discovered stable primary hostname')
                gate.reject(lambda: gate.client(primary, context=old_context, fresh=True), ssl.SSLCertVerificationError)
                gate.reject(lambda: gate.client(primary, server_name='wrong-host.invalid', fresh=True), ssl.SSLCertVerificationError)
                print('REDIS_TLS_ROTATION_PASS phases=trust,leaf,retire-old-ca '
                      'discovered-hostname=verified old-ca=denied pvc-uids=retained digest=verified', flush=True)
            else:
                old_password = gate.passwords['password']
                # Both credential consumers change within a bounded write pause.
                replace('redis-gate-acl', new['redis-gate-acl'])
                replace('redis-gate-auth', new['redis-gate-auth'])
                roll()
                primary = gate.primary()
                gate.reject(lambda: gate.client(primary, password=old_password, fresh=True), focused.RedisReplyError, 'WRONGPASS')
                gate.reject(lambda: gate.client(gate.members()['sentinel'][0]['metadata']['name'], 26379,
                                                password=old_password, fresh=True), focused.RedisReplyError, 'WRONGPASS')
                gate.security()
                print('REDIS_PASSWORD_ROTATION_PASS write-pause=declared old-redis-password=denied '
                      'old-sentinel-password=denied new-acl=verified pvc-uids=retained digest=verified', flush=True)
        except BaseException as exc:
            failure = exc
            recover(gate, rotation, roll)
            raise
        finally:
            try:
                gate.cleanup_keys()
            except Exception:
                if failure is None:
                    raise
                print('REDIS_ROTATION_KEY_CLEANUP_FAIL original failure retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
