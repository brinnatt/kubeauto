#!/usr/bin/env python3
"""Qualified Sentinel controller/schema/server migration through config plus kubecli."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
import signal
import tempfile
import time

import yaml

LEGACY = {'redis_operator_ver': '0.26.0', 'redis_operator_image_ver': '0.26.0', 'redis_ver': '8.10.1'}
CURRENT = {'redis_operator_ver': '0.27.0', 'redis_operator_image_ver': '0.27.0-kubeauto.2', 'redis_ver': '8.10.2'}


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version_input(original, versions, crd=False):
    config = yaml.safe_load(original)
    if (config.get('redis_namespace') != 'redis-gate' or config.get('redis_mode') != 'sentinel' or
            not set(versions) <= set(CURRENT) or
            any(value not in (CURRENT[key], LEGACY[key]) for key, value in versions.items())):
        raise ValueError('upgrade gate refuses a foreign namespace, mode or unqualified version')
    return yaml.safe_dump(dict(config, **versions, redis_crd_upgrade_requested=crd), sort_keys=False).encode()


def fixture_aliases(evidence):
    if (evidence.get('workflow_conclusion') != 'success' or evidence.get('durable_rc') != 0 or
            evidence.get('failure_markers') != 0):
        raise ValueError('upgrade fixtures require accepted publication')
    result = []
    for name, tag, alias in [('redis-operator-upgrade-fixture', '0.26.0', 'redis-operator'),
                             ('redis-upgrade-fixture', '8.10.1', 'redis')]:
        rows = [r for r in evidence['images'] if r['key'] == f'brinnatt/{name}:{tag}']
        if len(rows) != 1 or set(rows[0]['platforms']) != {'linux/amd64', 'linux/arm64'}:
            raise ValueError('upgrade fixture manifest coverage is incomplete')
        record = rows[0]
        if record['talkedu_ref'] != f'hub.talkedu.cn/kubeauto/{name}:{tag}':
            raise ValueError('upgrade fixture has a foreign registry mapping')
        if any(not re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in record['platforms'].values()):
            raise ValueError('upgrade fixture child digest is malformed')
        result.append((record['talkedu_ref'].rsplit(':', 1)[0] + '@' + record['platforms']['linux/amd64'],
                       f'127.0.0.1:5000/brinnatt/{alias}:{tag}', record['platforms']['linux/amd64']))
    return result


class Upgrade:
    def __init__(self, source, focused, inputs):
        self.source, self.focused, self.inputs = source, focused, inputs
        self.config = source.cluster / 'config.yml'
        self.original = self.config.read_bytes()
        self.accepted = [self.original]
        self.final = version_input(self.original, CURRENT)
        self.journal = source.cluster / 'redis-upgrade-fault.json'
        if self.journal.exists():
            raise ValueError('upgrade requires recovery of the prior version mutation')
        if source.get('rediscluster')['items']:
            raise ValueError('legacy Operator qualification requires a Sentinel-only fixture')
        self.claims = focused.resource_identities(source.get('pvc')['items'])
        self.expiry = int(time.time() * 1000) + 7200000
        self.chart_owned = []
        self.seeded = False
        self.changed = False

    def prepare_artifacts(self):
        evidence = json.loads((self.source.base / 'tests/evidence/redis-images-20261012.json').read_text())
        for origin, target, expected in fixture_aliases(evidence):
            # Local aliases are runner infrastructure, immutable by child digest.
            # They never change a published production or fixture tag.
            raw = subprocess.run(['skopeo', 'inspect', '--raw', 'docker://' + origin],
                                 capture_output=True, check=True, timeout=90).stdout
            if 'sha256:' + hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError('upgrade fixture delivery digest changed')
            existing = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false',
                                       'docker://' + target], capture_output=True, timeout=60)
            if existing.returncode == 0:
                if 'sha256:' + hashlib.sha256(existing.stdout).hexdigest() != expected:
                    raise ValueError('upgrade fixture alias collides with another cached image')
            else:
                if b'manifest unknown' not in existing.stderr.lower() and b'name unknown' not in existing.stderr.lower():
                    raise ValueError('upgrade fixture cache is unavailable')
                self.source.run(['skopeo', 'copy', '--preserve-digests', '--dest-tls-verify=false',
                                 'docker://' + origin, 'docker://' + target], timeout=900)
                actual = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false',
                                         'docker://' + target], capture_output=True, check=True, timeout=60).stdout
                if 'sha256:' + hashlib.sha256(actual).hexdigest() != expected:
                    raise ValueError('upgrade alias readback digest mismatch')
            print('REDIS_UPGRADE_FIXTURE_PASS alias=' + target + ' digest=' + expected, flush=True)
        chart = self.source.base / 'tests/fixtures/redis-operator-0.26.0.tgz'
        checksum = chart.with_suffix(chart.suffix + '.sha256')
        if hashlib.sha256(chart.read_bytes()).hexdigest() != checksum.read_text().split()[0]:
            raise ValueError('legacy official Chart checksum mismatch')
        for origin in (chart, checksum):
            target = self.source.base / 'roles/cluster-addon/files' / origin.name
            if target.exists():
                if target.read_bytes() != origin.read_bytes():
                    raise ValueError('legacy Chart installation refuses a foreign existing artifact')
            else:
                with target.open('xb') as out:
                    out.write(origin.read_bytes())
                self.chart_owned.append(target)

    def declare(self, versions, *, crd=False, marker=None):
        replacement = version_input(self.original, versions, crd)
        self.inputs.replace_config(self.config, self.accepted, replacement)
        self.accepted.append(replacement)
        self.changed = True
        self.source.run([str(self.source.base / '.venv/bin/python'), str(self.source.base / 'kubecli.py'),
                         'setup', 'redis-gate', '07', '--tags', 'redis'], timeout=1400, capture=False)
        operator_image = 'registry.talkschool.cn:5000/brinnatt/redis-operator:' + versions['redis_operator_image_ver']
        runtime_image = 'registry.talkschool.cn:5000/brinnatt/redis:' + versions['redis_ver']
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            operator = json.loads(self.source.k('-n', self.source.config['redis_operator_namespace'],
                                                'get', 'deployment', 'redis-operator', '-o', 'json'))
            pods = self.source.members()
            all_pods = [p for group in pods.values() for p in group]
            if (operator['spec']['template']['spec']['containers'][0]['image'] == operator_image and
                    operator.get('status', {}).get('updatedReplicas') == 2 and
                    operator.get('status', {}).get('readyReplicas') == 2 and len(all_pods) == 6 and
                    all(not p['metadata'].get('deletionTimestamp') and any(c['type'] == 'Ready' and c['status'] == 'True'
                        for c in p['status'].get('conditions', [])) and
                        any(c['name'] == p['metadata']['labels']['app'] and c['image'] == runtime_image for c in p['spec']['containers']) and
                        any(c['name'] == 'init-config' and c['image'] == operator_image for c in p['spec'].get('initContainers', []))
                        for p in all_pods)):
                break
            print('REDIS_UPGRADE_HEARTBEAT stage=version-rollout versions=' + json.dumps(versions), flush=True)
            time.sleep(3)
        else:
            raise ValueError('upgrade did not converge all data, Sentinel and agent image declarations')
        self.source.forwards.clear()
        self.source.health()
        if self.claims != self.focused.resource_identities(self.source.get('pvc')['items']):
            raise ValueError('version migration replaced a retained PVC')
        for pod in self.source.members()['replication']:
            client = self.source.client(pod['metadata']['name'], fresh=True)
            info = dict(l.split(':', 1) for l in client.command('INFO', 'server').decode().splitlines() if ':' in l)
            if info.get('redis_version') != versions['redis_ver']:
                raise ValueError('declared Redis image does not prove the actual server version')
            if self.seeded and client.command('GET', self.source.key) != self.source.value:
                raise ValueError('upgrade or rollback lost an acknowledged business value')
            if self.seeded:
                ttl = client.command('PTTL', self.source.key)
                if ttl <= 0 or abs(ttl - (self.expiry - int(time.time() * 1000))) > 5000:
                    raise ValueError('upgrade or rollback changed absolute expiry')
        if self.seeded:
            self.business()
        if marker:
            print(marker + ' product-entry=config+kubecli versions=' + json.dumps(versions) + ' data-and-pvc=retained', flush=True)

    def business(self):
        client = self.source.client(self.source.primary(), fresh=True)
        if (client.command('SET', self.source.key, self.source.value, 'PXAT', self.expiry) != b'OK' or
                client.command('WAITAOF', 1, 2, 5000) != [1, 2] or
                client.command('GET', self.source.key) != self.source.value):
            raise ValueError('version migration business path lacks three durable copies')

    def execute(self, prepare_only):
        self.prepare_artifacts()
        with self.journal.open('x') as out:
            self.journal.chmod(0o600)
            json.dump({'run_id': self.source.run_id, 'original_config_sha256': hashlib.sha256(self.original).hexdigest()}, out)
            out.flush()
            os.fsync(out.fileno())
        # This fixture starts on the installed forward schema. Downgrading its
        # controller/runtime must retain that schema, as supported rollback does.
        self.business()
        self.seeded = True
        self.declare(LEGACY, marker='REDIS_UPGRADE_LEGACY_BASELINE_PASS')
        self.business()
        if prepare_only:
            print('REDIS_UPGRADE_PREPARE_PASS acceptance=partial-no-migration', flush=True)
            return
        self.declare(dict(CURRENT, redis_ver='8.10.1'), crd=True, marker='REDIS_OPERATOR_VERSION_UPGRADE_PASS')
        self.declare(CURRENT, marker='REDIS_SERVER_VERSION_UPGRADE_PASS')
        # Controller rollback retains the forward CRD schema. Redis rollback is
        # qualified only for this same-series patch pair and this seeded format.
        self.declare(dict(LEGACY, redis_ver='8.10.2'), marker='REDIS_OPERATOR_SUPPORTED_ROLLBACK_PASS')
        self.declare(LEGACY, marker='REDIS_SERVER_SUPPORTED_ROLLBACK_PASS')
        self.declare(CURRENT, crd=True, marker='REDIS_UPGRADE_FINAL_CURRENT_PASS')
        print('REDIS_SENTINEL_VERSION_UPGRADE_PASS operator=0.26.0-to-0.27.0 '
              'redis=8.10.1-to-8.10.2 schema=explicit supported-rollback=qualified-pair', flush=True)

    def cleanup(self):
        if self.changed:
            # Verify retained business data on current CRD/controller/server,
            # then reset only the ephemeral request in the authoritative input.
            self.declare(CURRENT, crd=True)
            self.inputs.replace_config(self.config, self.accepted, self.final)
            self.accepted.append(self.final)
            self.source.cleanup_keys()
            if json.loads(self.journal.read_text())['run_id'] != self.source.run_id:
                raise ValueError('version cleanup refuses a foreign journal')
            self.journal.unlink()
        for chart in self.chart_owned:
            fixture = self.source.base / 'tests/fixtures' / chart.name
            if chart.read_bytes() != fixture.read_bytes():
                raise ValueError('version cleanup refuses a modified fixture artifact')
            chart.unlink()
        print('REDIS_UPGRADE_SCOPED_CLEAN_PASS requested=false target=current fixture-chart=removed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('upgrade-prepare', 'upgrade-sentinel'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_upgrade_focused')
    inputs = load(args.base, 'redis-capacity-gate.py', 'redis_upgrade_inputs')
    with tempfile.TemporaryDirectory(prefix='redis-upgrade-') as temporary, ExitStack() as stack:
        source = focused.Gate(args, Path(temporary), stack)
        source.baseline()
        gate = Upgrade(source, focused, inputs)
        failure = None
        try:
            gate.execute(args.module == 'upgrade-prepare')
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                gate.cleanup()
            except BaseException:
                if failure is None:
                    raise
                print('REDIS_UPGRADE_CLEANUP_FAIL original-failure=retained journal=retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
