#!/usr/bin/env python3
"""V1 storage faults share an owned confirmed-write ledger and recovery scope.

The source uses config+kubecli and its accepted business fixture. LONGHORN-39
keeps one real 1800-second fsync window across controller, remote worker and
replica-process faults. A remote worker reboot is not application rescheduling.
"""
import argparse
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

NS = 'longhorn-system'
RECORD = 'longhorn-install-business.json'
KEY = 'v1_storage_faults'
CASES = ('stability',)


def load(name):
    spec = importlib.util.spec_from_file_location('v1_fault_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def persist(lab, journal):
    lab.write_private(lab.DIRECTORY/RECORD, journal)


def address(lab, node):
    rows = [ip for ip in lab.WORKERS if node == 'longhorn-node-'+ip.rsplit('.', 1)[1]]
    if len(rows) != 1:
        raise ValueError('V1 fault node is outside the leased storage workers')
    return rows[0]


def host(lab, ip, *, unavailable=False):
    if ip not in lab.WORKERS:
        raise ValueError('V1 fault host is outside the lease')
    try:
        response = load('longhorn-v2-runtime-faults.py').host_call(lab, ip, {'action': 'host'}, check=not unavailable)
    except (TimeoutError, subprocess.TimeoutExpired):
        if unavailable:
            return None
        raise
    if unavailable and response.returncode == 255:
        return None
    if response.returncode:
        raise ValueError('V1 worker probe failed outside its expected SSH outage')
    row = json.loads(response.stdout)
    if not row.get('machine_id') or not row.get('boot_id'):
        raise ValueError('V1 worker lacks actual machine/boot identities')
    return row


def source(lab, journal):
    engine = load('longhorn-control-faults.py').source(lab, journal)
    volumes = lab.get('volumes.longhorn.io', namespace=NS)['items']
    if {v['metadata']['name']: v['metadata']['uid'] for v in volumes} != {journal['volume']: journal['volume_uid']}:
        raise ValueError('V1 shared fault requires one exclusively owned volume')
    lab.check_business_graph(journal)
    return engine


def nodes(lab):
    result = {}
    for ip in lab.WORKERS:
        name = 'longhorn-node-'+ip.rsplit('.', 1)[1]
        obj = lab.get('node', name)
        if (not obj or not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                for c in obj.get('status', {}).get('conditions', []))):
            raise ValueError('V1 fault requires all original worker nodes Ready')
        result[name] = {'uid': obj['metadata']['uid'], 'unschedulable': bool(obj['spec'].get('unschedulable')),
                        'taints': obj['spec'].get('taints', [])}
    return result


def node_restored(lab, name, saved):
    obj = lab.get('node', name)
    if not obj or obj['metadata']['uid'] != saved['uid']:
        raise ValueError('V1 reboot changed the original Kubernetes Node')
    if bool(obj['spec'].get('unschedulable')) != saved['unschedulable']:
        raise ValueError('V1 reboot changed the original scheduling policy')
    return (obj['spec'].get('taints', []) == saved['taints'] and any(c.get('type') == 'Ready' and c.get('status') == 'True'
            for c in obj.get('status', {}).get('conditions', [])))


class Probe:
    def __init__(self, lab, pid):
        self.lab = lab; self.pid = pid; self.journal = None; self.process = None; self.stream = None
        self.directory = None

    def emit(self, marker, row):
        print(marker+' '+json.dumps(row, sort_keys=True), flush=True)

    def remember(self):
        persist(self.lab, self.journal)

    def logs(self):
        path = self.directory/'ledger.log'
        if path.is_symlink() or not path.is_file():
            raise ValueError('V1 fault ledger is not its original private regular file')
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.startswith('{') and line.endswith('}')]

    def count(self):
        if self.process is not None and self.process.poll() is not None:
            if self.process.returncode or not any(r.get('type') == 'complete' for r in self.logs()):
                raise ValueError('V1 confirmed-load writer exited without normal completion')
        return sum(r.get('type') == 'ack' for r in self.logs())

    def start(self):
        self.directory = Path(tempfile.mkdtemp(prefix='kubeauto-longhorn-v1-faults-', dir='/var/tmp'))
        self.directory.chmod(0o700)
        proof = self.journal[KEY]
        proof.update(token=self.lab.owner()['run_id']+':v1-stability', log_directory=str(self.directory))
        self.remember()
        program = load('longhorn-v2-observability.py').sustained_program(proof['token'])
        self.stream = (self.directory/'ledger.log').open('w')
        self.process = subprocess.Popen([str(self.lab.BASE/'kube-bin/kubectl'),
            '--kubeconfig='+str(self.lab.DIRECTORY/'kubectl.kubeconfig'), '-n', self.journal['namespace'],
            'exec', 'writer', '--', 'python3', '-u', '-c', program],
            stdin=subprocess.DEVNULL, stdout=self.stream, stderr=self.stream)
        self.lab.wait('v1-stability-first-four-fsync-confirmations', 90, lambda: self.count() >= 4)
        proof['writer_start'] = next(r for r in self.logs() if r.get('type') == 'started')
        self.remember()

    def begin(self, kind, row):
        row = dict(row, kind=kind, intent=True, before_acks=self.count())
        self.journal[KEY]['faults'].append(row)
        self.remember()
        return row

    def finish(self, row, observations):
        at_recovery = self.count()
        self.lab.wait('v1-'+row['kind']+'-post-recovery-fsync', 90, lambda: self.count() >= at_recovery+4)
        row.update(recovered=True, after_acks=self.count(), observations=observations)
        self.remember()
        self.emit('LONGHORN_V1_STORAGE_FAULT_RECOVERED', row)

    def replica(self):
        lab = self.lab; journal = self.journal
        engine = source(lab, journal)
        before = lab.replica_topology(journal, 3)
        remote = sorted([r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
            if r['spec'].get('volumeName') == journal['volume'] and r['spec'].get('nodeID') != engine['node']],
            key=lambda r: r['metadata']['name'])
        if len(remote) != 2:
            raise ValueError('V1 replica process fault lacks two original remote replicas')
        faults = load('longhorn-faults.py')
        native = faults.replica_process(lab, journal, remote[0])
        row = self.begin('replica-process', {'native': native, 'before': before})
        actual = faults.stop_replica_process(lab, journal, remote[0])
        if actual != native:
            raise ValueError('V1 native replica UUID changed before deletion')
        def failed():
            obj = lab.get('replicas.longhorn.io', native['name'], NS)
            return not obj or obj['metadata']['uid'] != native['replica_uid'] or bool(obj['spec'].get('failedAt'))
        lab.wait('v1-stability-native-replica-process-failed', 180, failed)
        after = lab.wait('v1-stability-three-independent-replicas-rebuilt', 1200, lambda: lab.replica_topology(journal, 3))
        if any(after.get(name) != saved for name, saved in before.items() if name != native['name']):
            raise ValueError('V1 single-replica fault replaced a healthy survivor')
        self.finish(row, {'replicas': after, 'original_native_uuid_deleted': True})

    def manager(self):
        lab = self.lab; journal = self.journal
        control = load('longhorn-control-faults.py')
        engine = source(lab, journal)
        pod, parent = control.fault_pod(lab, journal, 'manager', engine)
        row = self.begin('controller', {'variant': 'manager', 'engine': engine, 'parent': parent,
            'pod': pod['metadata']['name'], 'pod_uid': pod['metadata']['uid']})
        current = lab.get('pod', row['pod'], NS)
        if not current or current['metadata']['uid'] != row['pod_uid']:
            raise ValueError('V1 manager changed before its UID-preconditioned fault')
        lab.remove(current)
        lab.wait('v1-stability-owned-controller-recreated', 600, lambda: control.recovered(lab, journal, row))
        candidates = [p for p in lab.get('pods', namespace=NS)['items']
            if p['spec'].get('nodeName') == parent['node'] and control.controlled(p, parent['kind'], parent['uid'])]
        if (len(candidates) != 1 or candidates[0]['metadata']['uid'] == row['pod_uid'] or
                not control.ready(candidates[0])):
            raise ValueError('V1 controller did not obtain a healthy replacement identity')
        current = candidates[0]
        self.finish(row, {'pod_uid': current['metadata']['uid'], 'replicas': lab.replica_topology(journal, 3)})

    def worker(self):
        lab = self.lab; journal = self.journal
        engine = source(lab, journal)
        remote = sorted(n for n in journal[KEY]['nodes'] if n != engine['node'])[0]
        ip = address(lab, remote); original = host(lab, ip)
        row = self.begin('worker-reboot', {'address': ip, 'node': remote, 'original_host': original})
        load('longhorn-v1-reboot-relay.py').reboot(lab, self.pid, ip, original)
        def restored():
            current = host(lab, ip, unavailable=True)
            if current is None or current['boot_id'] == original['boot_id']:
                return None
            if current['machine_id'] != original['machine_id']:
                raise ValueError('V1 worker reboot changed physical machine identity')
            if not node_restored(lab, remote, journal[KEY]['nodes'][remote]):
                return None
            topology = lab.replica_topology(journal, 3)
            return {'host': current, 'replicas': topology} if topology else None
        observed = lab.wait('v1-stability-actual-new-worker-boot-and-rebuild', 1200, restored)
        self.finish(row, observed)

    def run(self, journal):
        self.journal = journal
        if KEY in journal:
            raise ValueError('prior V1 fault workload requires scoped recovery')
        source(self.lab, journal)
        journal[KEY] = {'run_id': self.lab.owner()['run_id'], 'cluster_uid': self.lab.owner()['cluster_uid'],
            'volume_uid': journal['volume_uid'], 'case': 'stability', 'nodes': nodes(self.lab), 'faults': []}
        self.remember(); self.start()
        self.manager(); self.worker(); self.replica()
        last = time.monotonic()
        while self.process.poll() is None:
            now = time.monotonic()
            if now-last >= 15:
                self.emit('LONGHORN_V1_STABILITY_HEARTBEAT', {'confirmed': self.count(),
                    'remaining_s': max(0, 1800-self.logs()[-1].get('observed_elapsed_s', 0))})
                last = now
            time.sleep(1)
        if self.process.returncode:
            raise ValueError('V1 thirty-minute confirmed writer did not exit normally')
        proof = load('longhorn-v2-observability.py').confirmed_load(self.logs(), journal[KEY]['token'])
        load('longhorn-faults.py').verify_client_ledger(self.lab, journal, 'writer', proof)
        source(self.lab, journal)
        for name, saved in journal[KEY]['nodes'].items():
            self.lab.wait('v1-stability-original-node-policy-'+name, 180, lambda: node_restored(self.lab, name, saved))
        journal[KEY]['confirmed'] = proof; self.remember()
        self.emit('LONGHORN_V1_STABILITY_ACKNOWLEDGEMENTS', {'token': proof['token'], 'rows': self.logs()})
        self.emit('LONGHORN_V1_STABILITY_RESULT', {k: proof[k] for k in
            ('summary', 'timeline', 'errors', 'retries', 'max_latency_s')})

    def restore(self):
        if self.journal is None:
            return
        proof = self.journal.get(KEY)
        if (not proof or any(proof.get(k) != self.lab.owner()[k] for k in ('run_id', 'cluster_uid')) or
                proof.get('volume_uid') != self.journal.get('volume_uid')):
            raise ValueError('V1 fault cleanup belongs to another source/cluster')
        for row in proof['faults']:
            if row['kind'] == 'worker-reboot':
                def ready():
                    current = host(self.lab, row['address'], unavailable=True)
                    if current is None:
                        return False
                    if current['machine_id'] != row['original_host']['machine_id']:
                        raise ValueError('V1 reboot recovery machine changed')
                    return node_restored(self.lab, row['node'], proof['nodes'][row['node']])
                self.lab.wait('v1-fault-worker-restored-before-cleanup', 1200, ready)
        self.lab.wait('v1-fault-three-replicas-before-cleanup', 1200, lambda: self.lab.replica_topology(self.journal, 3))
        self.lab.installation_health()
        pod = self.lab.get('pod', 'writer', self.journal['namespace'])
        if pod:
            if pod['metadata']['uid'] != self.journal['objects'].get('Pod/writer'):
                raise ValueError('V1 fault cleanup refuses a replaced writer')
            if proof.get('token'):
                directory = Path(proof['log_directory'])
                if (directory.parent != Path('/var/tmp') or not directory.name.startswith('kubeauto-longhorn-v1-faults-') or
                        directory.is_symlink() or directory.stat().st_uid != os.geteuid()):
                    raise ValueError('V1 fault cleanup refuses an unowned private ledger directory')
                self.directory = directory
                acknowledgements = [r for r in self.logs() if r.get('type') == 'ack']
                if acknowledgements:
                    records = [{k: v for k, v in r.items() if k != 'observed_elapsed_s'} for r in acknowledgements]
                    faults = load('longhorn-faults.py')
                    checked = load('longhorn-control-faults.py').confirmed(records, proof['token'], faults)
                    faults.verify_client_ledger(self.lab, self.journal, 'writer', checked)
            self.lab.remove(pod)
            self.lab.wait('v1-fault-original-writer-fenced-before-cleanup', 180,
                lambda: self.lab.get('pod', 'writer', self.journal['namespace']) is None)
        print('LONGHORN_V1_STORAGE_FAULT_RESTORED_BEFORE_CLEANUP', flush=True)


def business(lab, pid, case):
    if case not in CASES:
        raise ValueError('unsupported V1 storage-fault case')
    probe = Probe(lab, pid)
    original_performance, original_cleanup, original_kubectl = lab.verify_performance, lab.cleanup_business, lab.kubectl
    def cleanup(record=None, **kwargs):
        probe.restore()
        return original_cleanup(record, **kwargs)
    def kubectl(*args, **kwargs):
        result = original_kubectl(*args, **kwargs)
        if ('exec' in args and 'remount-reader' in args and 'python3' in args and probe.journal and
                probe.journal.get(KEY, {}).get('confirmed') and any(
                    isinstance(a, str) and "'/data/confirmed'" in a for a in args)):
            from types import SimpleNamespace
            proof = probe.journal[KEY]['confirmed']
            load('longhorn-faults.py').verify_client_ledger(SimpleNamespace(kubectl=original_kubectl),
                probe.journal, 'remount-reader', proof)
            probe.emit('LONGHORN_V1_STABILITY_REMOUNT', proof['summary'])
        return result
    lab.verify_performance = lambda journal, payload, digest: probe.run(journal)
    lab.cleanup_business = cleanup; lab.kubectl = kubectl
    try:
        lab.run_requests(pid, [])
        lab.business('performance', relay_pid=pid, storage_class='longhorn-delete')
    finally:
        lab.verify_performance, lab.cleanup_business, lab.kubectl = original_performance, original_cleanup, original_kubectl
        if probe.process is not None and probe.process.poll() is None:
            probe.process.terminate()
            try: probe.process.wait(timeout=10)
            except subprocess.TimeoutExpired: probe.process.kill(); probe.process.wait(timeout=10)
        if probe.stream is not None: probe.stream.close()
        if probe.directory: print('LONGHORN_PRIVATE_DIAGNOSTIC directory='+str(probe.directory), flush=True)
    print('LONGHORN_V1_STABILITY_BUSINESS_PASS', flush=True)


def cleanup(lab, pid):
    load('longhorn-v1-reboot-relay.py').recover_current(lab)
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('V1 faults refuse symlink business recovery')
    if path.exists():
        journal = json.loads(path.read_text())
        if KEY not in journal:
            raise ValueError('V1 storage-fault cleanup cannot delete another module')
        lab.check_business_graph(journal)
        probe = Probe(lab, pid); probe.journal = journal
        probe.restore()
        lab.cleanup_business(relay_pid=pid)
    print('LONGHORN_V1_STABILITY_SCOPED_CLEAN_PASS', flush=True)


def baseline(lab):
    load('longhorn-platform.py').empty(lab)
    lab.installation_health()
    nodes(lab)
    print('LONGHORN_V1_STABILITY_BASELINE_VERIFIED', flush=True)


def verify_log(log, case):
    if case not in CASES or re.search(r'^(LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', log, re.M):
        raise ValueError('V1 fault log has an unsupported scope or failure marker')
    def one(marker):
        rows = [json.loads(line[len(marker)+1:]) for line in log.splitlines() if line.startswith(marker+' ')]
        if len(rows) != 1:
            raise ValueError('V1 fault requires one complete observation: '+marker)
        return rows[0]
    ledger = one('LONGHORN_V1_STABILITY_ACKNOWLEDGEMENTS')
    if not ledger.get('token', '').endswith(':v1-stability'):
        raise ValueError('V1 stability token differs')
    proof = load('longhorn-v2-observability.py').confirmed_load(ledger['rows'], ledger['token'])
    if (one('LONGHORN_V1_STABILITY_RESULT') != {k: proof[k] for k in
            ('summary', 'timeline', 'errors', 'retries', 'max_latency_s')} or
            one('LONGHORN_V1_STABILITY_REMOUNT') != proof['summary']):
        raise ValueError('V1 stability raw fsync prefix, final result and remount differ')
    rows = [json.loads(line.split(' ', 1)[1]) for line in log.splitlines()
            if line.startswith('LONGHORN_V1_STORAGE_FAULT_RECOVERED ')]
    if [r.get('kind') for r in rows] != ['controller', 'worker-reboot', 'replica-process']:
        raise ValueError('V1 stability requires controller, worker and replica faults')
    prior_ack = 0
    for row in rows:
        topology = row.get('observations', {}).get('replicas', {})
        if (row.get('intent') is not True or row.get('recovered') is not True or
                not max(4, prior_ack) <= row.get('before_acks', -1) <= row.get('after_acks', -1)-4 or
                not row['after_acks'] <= proof['summary']['confirmed'] or
                len(topology) != 3 or len({r.get('node') for r in topology.values()}) != 3 or
                len({r.get('uid') for r in topology.values() if r.get('uid')}) != 3):
            raise ValueError('V1 stability lacks the fault/recovery write prefix and full replica set')
        prior_ack = row['after_acks']
        if row['kind'] == 'controller':
            if not row.get('pod_uid') or row['pod_uid'] == row['observations'].get('pod_uid'):
                raise ValueError('V1 controller fault lacks a new healthy Pod UID')
        elif row['kind'] == 'worker-reboot':
            first, last = row['original_host'], row['observations']['host']
            if first['machine_id'] != last['machine_id'] or first['boot_id'] == last['boot_id']:
                raise ValueError('V1 worker fault lacks an actual same-machine new boot')
        else:
            if not row.get('native', {}).get('uuid') or row['observations'].get('original_native_uuid_deleted') is not True:
                raise ValueError('V1 replica fault lacks its native UUID deletion')
            if any(topology.get(k) != v for k, v in row['before'].items() if k != row['native']['name']):
                raise ValueError('V1 replica fault changed healthy survivor identities')
    for marker in ('LONGHORN_V1_STABILITY_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                   'LONGHORN_V1_STORAGE_FAULT_RESTORED_BEFORE_CLEANUP'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('V1 stability terminal/cleanup contract incomplete: '+marker)
    print('LONGHORN_V1_STABILITY_LOG_VERIFIED', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'cleanup', 'verify', 'baseline'))
    parser.add_argument('--case', choices=CASES, default='stability')
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log: parser.error('verify requires --log')
        verify_log(args.log.read_text(), args.case)
        return
    lab = load('longhorn-lab.py'); lab.require_lease()
    if args.mode == 'baseline':
        baseline(lab)
        return
    if args.relay_pid <= 0: parser.error('V1 storage faults require a supervised relay')
    if args.mode == 'business': business(lab, args.relay_pid, args.case)
    else: cleanup(lab, args.relay_pid)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda number, frame: sys.exit(128+number))
    try: main()
    except Exception:
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v1-fault-', suffix='.txt', dir='/var/tmp', delete=False) as stream:
            traceback.print_exc(file=stream)
            print('LONGHORN_V1_STORAGE_FAULT_FAIL private_diagnostic='+stream.name, file=sys.stderr)
        sys.exit(1)
