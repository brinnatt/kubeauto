#!/usr/bin/env python3
"""Exercise the actual Redis cleanup CLI on success, refusal and interruption."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def namespace(name, run_id, owned=True):
    if not name.startswith('redis-clean-'):
        raise ValueError('cleanup qualification names must remain within their fixture prefix')
    return {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name, 'labels': {
        'kubeauto.io/component': 'redis' if owned else 'redis-clean-canary',
        'kubeauto.io/managed-by': 'kubeauto', 'kubeauto.io/test-run': run_id}}}


def exact_delete(source, obj):
    metadata = obj['metadata']
    if metadata.get('labels', {}).get('kubeauto.io/test-run') != source.run_id:
        raise ValueError('cleanup qualification refuses a foreign fixture identity')
    path = '/api/v1/namespaces/' + metadata['name']
    source.k('delete', '--raw', path, '-f', '-', input_text=json.dumps({
        'apiVersion':'v1', 'kind':'DeleteOptions', 'preconditions':{
            'uid':metadata['uid'], 'resourceVersion':metadata['resourceVersion']}}))


class Qualification:
    def __init__(self, source, work):
        self.source, self.work = source, work
        prefix = 'redis-clean-' + source.run_id[:12]
        self.data, self.operator, self.canary = (prefix + '-' + suffix for suffix in ('data', 'op', 'canary'))
        self.script = source.base / 'tests/helpers/redis-cleanup.sh'
        self.env = {**os.environ, 'KUBECONFIG':str(source.kubeconfig), 'REDIS_NAMESPACE':self.data,
                    'REDIS_OPERATOR_NAMESPACE':self.operator,
                    'REDIS_CRD_OWNERSHIP_FILE':str(work/'absent-crd-journal'),
                    'REDIS_HELM':str(source.base/'extra-bin/helm')}
        self.owned = {}
        self.blocker = None
        self.process = None

    def create(self, name, owned=True):
        if self.source.k('get', 'namespace', name, '--ignore-not-found', '-o', 'name'):
            raise ValueError('cleanup qualification fixture already exists')
        self.source.k('create', '-f', '-', input_text=json.dumps(namespace(name, self.source.run_id, owned)))
        obj = json.loads(self.source.k('get', 'namespace', name, '-o', 'json'))
        self.owned[name] = obj['metadata']['uid']
        return obj

    def cli(self):
        return subprocess.run(['bash', str(self.script)], env=self.env, cwd=self.source.base,
                              capture_output=True, text=True, timeout=250)

    def absent(self):
        for name in (self.data, self.operator):
            if self.source.k('get', 'namespace', name, '--ignore-not-found', '-o', 'name'):
                raise ValueError('cleanup qualification scoped namespace remains')
            self.owned.pop(name, None)
        current = json.loads(self.source.k('get', 'namespace', self.canary, '-o', 'json'))
        if current['metadata']['uid'] != self.owned[self.canary]:
            raise ValueError('Redis cleanup altered its out-of-scope canary')

    def execute(self):
        self.create(self.canary, False)
        self.create(self.data)
        self.create(self.operator)
        result = self.cli()
        if result.returncode or 'REDIS_CLEANUP_PASS' not in result.stdout or 'REDIS_CLEAN_VERIFY_PASS' not in result.stdout:
            raise ValueError('actual Redis cleanup CLI failed the normal scoped scenario')
        self.absent()
        print('REDIS_CLEANUP_NORMAL_PASS entry=redis-cleanup.sh foreign-namespace=retained', flush=True)
        self.create(self.data)
        foreign = self.create(self.operator, False)
        result = self.cli()
        if result.returncode == 0 or 'refusing foreign Redis namespace' not in result.stderr:
            raise ValueError('Redis cleanup did not refuse its deliberately foreign namespace boundary')
        data = json.loads(self.source.k('get', 'namespace', self.data, '-o', 'json'))
        if data['metadata']['uid'] != self.owned[self.data] or data['status']['phase'] != 'Active':
            raise ValueError('cleanup mutated the first namespace before validating the foreign second namespace')
        print('REDIS_CLEANUP_REFUSAL_PASS foreign-boundary=rejected before-mutation=true '
              'diagnostic-sha256=' + hashlib.sha256(result.stderr.encode()).hexdigest(), flush=True)
        exact_delete(self.source, foreign)
        self.source.k('wait', '--for=delete', 'namespace/' + self.operator, '--timeout=90s', timeout=110)
        self.owned.pop(self.operator)
        result = self.cli()
        if result.returncode:
            raise ValueError('refused cleanup did not recover through its same CLI after removing the owned foreign fixture')
        self.absent()
        self.create(self.data)
        self.create(self.operator)
        blocker = {'apiVersion':'v1', 'kind':'ConfigMap', 'metadata':{
            'name':'redis-cleanup-probe', 'namespace':self.data,
            'finalizers':['kubeauto.io/redis-cleanup-probe'],
            'labels':{'kubeauto.io/test-run':self.source.run_id}}, 'data':{'verified':'retained'}}
        self.source.k('create', '-f', '-', input_text=json.dumps(blocker))
        self.blocker = True
        with (self.work/'interrupted-cleanup.log').open('w+') as log:
            self.process = subprocess.Popen(['bash', str(self.script)], env=self.env, cwd=self.source.base,
                                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                obj = json.loads(self.source.k('get', 'namespace', self.data, '-o', 'json'))
                if obj['status']['phase'] == 'Terminating':
                    break
                if self.process.poll() is not None:
                    raise ValueError('cleanup process exited before reaching the held namespace deletion')
                time.sleep(0.5)
            else:
                raise ValueError('cleanup interruption fixture never reached its held deletion')
            os.killpg(self.process.pid, signal.SIGTERM)
            rc = self.process.wait(timeout=20)
            if rc == 0:
                raise ValueError('interrupted cleanup incorrectly returned success')
            self.process = None
        self.release_blocker()
        result = self.cli()
        if result.returncode or 'REDIS_CLEANUP_PASS' not in result.stdout:
            raise ValueError('interrupted cleanup did not resume through the same cleanup CLI')
        self.absent()
        print('REDIS_CLEANUP_INTERRUPT_PASS actual-cli=terminated resume=clean foreign-namespace=retained', flush=True)
        print('REDIS_CLEANUP_QUALIFICATION_PASS normal=verified failure=refused interruption=resumed', flush=True)

    def release_blocker(self):
        if not self.blocker:
            return
        raw = self.source.k('-n', self.data, 'get', 'configmap', 'redis-cleanup-probe', '--ignore-not-found', '-o', 'json')
        if raw:
            obj = json.loads(raw)
            if obj['metadata'].get('labels', {}).get('kubeauto.io/test-run') != self.source.run_id:
                raise ValueError('cleanup blocker identity changed')
            self.source.k('-n', self.data, 'patch', 'configmap', 'redis-cleanup-probe', '--type=merge', '-p',
                          json.dumps({'metadata': {'uid':obj['metadata']['uid'],
                          'resourceVersion':obj['metadata']['resourceVersion'], 'finalizers':[]}}))
        self.blocker = None

    def cleanup(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=20)
        self.release_blocker()
        for name, uid in list(self.owned.items()):
            raw = self.source.k('get', 'namespace', name, '--ignore-not-found', '-o', 'json')
            if raw:
                obj = json.loads(raw)
                if obj['metadata']['uid'] != uid:
                    raise ValueError('cleanup qualification namespace UID changed')
                exact_delete(self.source, obj)
                self.source.k('wait', '--for=delete', 'namespace/' + name, '--timeout=90s', timeout=110)
        print('REDIS_CLEANUP_FIXTURES_CLEAN_PASS namespaces=absent owned-processes=reaped', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('cleanup-qualification',))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_cleanup_focused')
    with tempfile.TemporaryDirectory(prefix='redis-cleanup-qualification-') as tmp, ExitStack() as stack:
        source = focused.Gate(args, Path(tmp), stack)
        source.baseline()
        gate = Qualification(source, Path(tmp))
        failure = None
        try:
            gate.execute()
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                gate.cleanup()
            except BaseException:
                if failure is None:
                    raise
                print('REDIS_CLEANUP_FIXTURES_FAIL original-failure=retained', flush=True)
        source.baseline()
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
