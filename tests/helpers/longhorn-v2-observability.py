#!/usr/bin/env python3
"""V2 observations share one bounded 30-minute confirmed-write window.

Metrics come from the real product Prometheus installation. A source-volume
replica transport fault leaves the monitoring volumes outside its endpoint
boundary. Capacity, bounded attach queues and workload/resource observations
share this owner; neither official GA nor a Ready Pod is a project result.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import inspect
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import zipfile

NS = 'longhorn-system'
# Lab diagnostic budgets; the official kit has no 64 MiB archive ceiling.
# Stream to a private file, retain >=1 GiB disk headroom and bound CRC work.
SUPPORT_COMPRESSED_BYTES = 1024**3
SUPPORT_EXPANDED_BYTES = 4*1024**3
SUPPORT_DISK_HEADROOM = 1024**3

RECORD = 'longhorn-install-business.json'
KEY = 'v2_observability'
LOAD_SECONDS = 1800
FILLER_BYTES = 512*1024**2


def client_pod(factory, *args, **kwargs):
    pod = factory(*args, **kwargs)
    if pod['metadata']['name'] in ('writer', 'remount-reader'):
        # Charge buffered file pages as well as Python RSS: 512Mi pressure,
        # <=450Mi confirmed ledger, 64Mi benchmark and bounded process margin.
        resources = pod['spec']['containers'][0]['resources']
        for kind in ('requests', 'limits'):
            resources[kind]['memory'] = '1536Mi'
    return pod


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sustained_program(token, duration=LOAD_SECONDS):
    faults = load('longhorn-faults.py')
    if not isinstance(token, str) or not token or type(duration) not in (int, float) or not 0 < duration <= LOAD_SECONDS:
        raise ValueError('invalid confirmed-load program')
    # Retain the accepted fsync/retry and byte-verification implementation.
    # Only its output function adds monotonic observations; the internal
    # acknowledgement records used for readback remain unchanged.
    source = inspect.getsource(faults.write_ledger)
    if source.count('print(') != 3:
        raise ValueError('accepted ledger emission contract changed')
    source = source.replace('print(', 'emit(')
    return ('import builtins,errno,hashlib,json,os,stat,time\nBLOCK=65536\n'
        'started=time.monotonic()\n'
        'def emit(text,**kw):\n row=json.loads(text);row["observed_elapsed_s"]=time.monotonic()-started;'
        'builtins.print(json.dumps(row,sort_keys=True),**kw)\n'+
        inspect.getsource(faults.ledger_block)+'\n'+inspect.getsource(faults.verify_ledger)+'\n'+source+
        '\nprint(json.dumps({"type":"started","token":%r,"pid":os.getpid(),'
        '"start_tick":open("/proc/self/stat").read().rsplit(")",1)[1].split()[19]}),flush=True)\n' % token+
        'write_ledger("/data/fault-ledger",%r,duration=%r,interval=.25,stop=None)\n' % (token, duration))


def confirmed_load(rows, token, *, minimum_seconds=LOAD_SECONDS):
    faults = load('longhorn-faults.py')
    starts = [r for r in rows if r.get('type') == 'started']
    complete = [r for r in rows if r.get('type') == 'complete']
    if (len(starts) != 1 or starts[0].get('token') != token or len(complete) != 1 or
            type(starts[0].get('pid')) is not int or starts[0]['pid'] <= 0 or
            not str(starts[0].get('start_tick', '')).isdigit() or rows[0] != starts[0] or rows[-1] != complete[0] or
            any(r.get('type') not in ('started', 'ack', 'retry', 'complete') for r in rows)):
        raise ValueError('confirmed load lacks one owned start and normal completion')
    acks = [r for r in rows if r.get('type') == 'ack']
    if len(acks) < 4:
        raise ValueError('confirmed load contains too few acknowledged writes')
    previous = -1.; records = []
    for row in acks:
        elapsed = row.get('observed_elapsed_s')
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < previous:
            raise ValueError('confirmed load monotonic acknowledgement timeline is invalid')
        previous = elapsed
        records.append({k: v for k, v in row.items() if k != 'observed_elapsed_s'})
    proof = load('longhorn-control-faults.py').confirmed(records, token, faults)
    ending = complete[0]
    errors = {}
    previous = -1.
    for row in rows[1:]:
        elapsed = row.get('observed_elapsed_s')
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < previous:
            raise ValueError('confirmed load emission timeline changed order')
        previous = elapsed
        if row['type'] == 'retry':
            if (row.get('errno') not in ('EIO', 'ETIMEDOUT', 'ESTALE', 'EROFS') or
                    type(row.get('sequence')) is not int or not 0 <= row['sequence'] <= len(acks)):
                raise ValueError('confirmed load retry boundary differs')
            errors[row['errno']] = errors.get(row['errno'], 0)+1
    end = ending.get('observed_elapsed_s')
    if (type(end) not in (int, float) or not math.isfinite(end) or end < minimum_seconds or
            any(ending.get(k) != v for k, v in proof['summary'].items()) or
            acks[-1]['observed_elapsed_s']-acks[0]['observed_elapsed_s'] < max(0, minimum_seconds-5) or
            any(not 0 <= r['observed_elapsed_s'] <= end for r in acks) or
            ending.get('errors') != errors or
            any(type(v) is not int or v < 0 for v in ending['errors'].values()) or
            ending.get('retries') != sum(ending['errors'].values()) or
            type(ending.get('max_latency_s')) not in (int, float) or
            not math.isfinite(ending['max_latency_s']) or ending['max_latency_s'] < 0):
        raise ValueError('confirmed-write duration, final prefix or final hash is incomplete')
    return dict(proof, timeline={'first_ack_s': acks[0]['observed_elapsed_s'],
        'last_ack_s': acks[-1]['observed_elapsed_s'], 'complete_s': ending['observed_elapsed_s'],
        'minimum_seconds': minimum_seconds}, errors=ending['errors'], retries=ending['retries'],
        max_latency_s=ending['max_latency_s'])


def queue_workload(directory, token, size, depth, *, operations=128):
    """Bounded buffered pwrite+fsync; report actual concurrent syscall windows."""
    if size not in (4096, 128*1024) or depth not in (1, 8, 32) or not 1 <= operations <= 512:
        raise ValueError('queue workload exceeds its size/depth/operation boundary')
    path = Path(directory)/('queue-'+str(size)+'-'+str(depth))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    original = os.fstat(fd); lock = threading.Lock(); active = peak = 0
    latencies = []; expected = {}; started = time.monotonic()
    def operation(index):
        nonlocal active, peak
        data = hashlib.sha256((token+':'+str(index)).encode()).digest()*(size//32)
        with lock: active += 1; peak = max(peak, active)
        begin = time.monotonic_ns()
        try:
            if os.pwrite(fd, data, index*size) != size:
                raise ValueError('short queued fsync write')
            os.fsync(fd)
            elapsed = time.monotonic_ns()-begin
            with lock:
                latencies.append(elapsed/1e6); expected[index] = hashlib.sha256(data).hexdigest()
        finally:
            with lock: active -= 1
    try:
        with ThreadPoolExecutor(max_workers=depth) as pool:
            list(pool.map(operation, range(operations)))
        os.fsync(fd)
        for index, digest in expected.items():
            if hashlib.sha256(os.pread(fd, size, index*size)).hexdigest() != digest:
                raise ValueError('queued workload confirmed bytes changed')
        ordered = sorted(latencies)
        return {'size_bytes': size, 'requested_depth': depth, 'measured_peak_inflight': peak,
            'operations': operations, 'verified_operations': len(expected), 'errors': 0,
            'elapsed_s': time.monotonic()-started, 'fsync_per_write': True,
            'latency_ms': {str(p): ordered[math.ceil(len(ordered)*p/100)-1] for p in (50, 95, 99)},
            'boundary': 'Buffered filesystem syscall concurrency, not an NVMe controller queue-depth or production throughput claim.'}
    finally:
        os.close(fd)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise ValueError('queued workload path replaced; refusing cleanup')
        path.unlink()


def iobuf_delta(before, after):
    def index(rows):
        result = {}
        if not isinstance(rows, list) or not rows:
            raise ValueError('native iobuf stats are empty')
        for row in rows:
            name = row['module']
            if not name or name in result:
                raise ValueError('native iobuf module identity is ambiguous')
            result[name] = {}
            for pool in ('small_pool', 'large_pool'):
                result[name][pool] = {}
                for key in ('cache', 'main', 'retry'):
                    value = row[pool][key]
                    if type(value) is not int or value < 0:
                        raise ValueError('native iobuf counters are not unsigned integers')
                    result[name][pool][key] = value
        return result
    first, last = index(before), index(after)
    if set(first) != set(last):
        raise ValueError('native iobuf modules changed during the workload')
    result = {name: {pool: {key: last[name][pool][key]-first[name][pool][key]
        for key in ('cache', 'main', 'retry')} for pool in ('small_pool', 'large_pool')} for name in first}
    if any(value < 0 for pools in result.values() for values in pools.values() for value in values.values()):
        raise ValueError('native iobuf counters reset; the SPDK process is not comparable')
    return result


def rpc_program():
    return '''import json,socket
results={}
for method,params in [('framework_get_config',{'name':'iobuf'}),('iobuf_get_stats',None)]:
 s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(10);s.connect('/var/tmp/spdk.sock')
 request={'jsonrpc':'2.0','id':1,'method':method}
 if params is not None:request['params']=params
 s.sendall(json.dumps(request).encode()+b'\\n');data=b''
 try:
  while True:
   block=s.recv(65536)
   if not block:raise ValueError('native iobuf RPC closed before a complete response')
   data+=block
   if len(data)>1024**2:raise ValueError('native iobuf RPC response exceeds bound')
   try:reply=json.loads(data);break
   except json.JSONDecodeError:pass
 finally:s.close()
 if reply.get('id')!=1 or 'error' in reply:raise ValueError('native iobuf RPC failed')
 results[method]=reply['result']
print(json.dumps(results,sort_keys=True))
'''


def support_archive(raw):
    """Hash and CRC-check a bounded private stream without loading or extracting it."""
    stream = io.BytesIO(raw) if isinstance(raw, bytes) else raw
    stream.seek(0, os.SEEK_END); size = stream.tell(); stream.seek(0)
    if not 0 < size <= SUPPORT_COMPRESSED_BYTES:
        raise ValueError('support bundle compressed size is outside its private fixture budget')
    digest = hashlib.sha256()
    while block := stream.read(65536):
        digest.update(block)
    stream.seek(0)
    with zipfile.ZipFile(stream) as archive:
        rows = archive.infolist()
        if (not rows or len(rows) > 10000 or len({r.filename for r in rows}) != len(rows) or
                sum(r.file_size for r in rows) > SUPPORT_EXPANDED_BYTES):
            raise ValueError('support bundle member count/duplicates/expanded size exceed the boundary')
        for row in rows:
            path = Path(row.filename)
            if (path.is_absolute() or '\\' in row.filename or any(p in ('', '.', '..') for p in row.filename.rstrip('/').split('/')) or
                    row.flag_bits & 1 or stat.S_ISLNK(row.external_attr >> 16)):
                raise ValueError('support bundle contains unsafe member metadata')
        if archive.testzip() is not None:
            raise ValueError('support bundle CRC verification failed')
        return {'compressed_bytes': size, 'expanded_bytes': sum(r.file_size for r in rows),
            'members': len(rows), 'sha256': digest.hexdigest(), 'crc_verified': True,
            'extracted': False, 'published': False,
            'boundary': 'Private diagnostic bundle, streamed to a mode-0600 temporary file and CRC-checked without extraction. '
                        'Lab limits are 1 GiB compressed / 4 GiB expanded with 1 GiB disk headroom, not upstream product limits. '
                        'No claim of automatic credential or personal-data redaction. Only size/count/checksum metadata enter evidence.'}


def download_support(lab, path, expected_bytes):
    """Receive the exact native response into a bounded private temporary file."""
    if (not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or
            not 0 < expected_bytes <= SUPPORT_COMPRESSED_BYTES):
        raise ValueError('support download size exceeds its private fixture budget')
    with tempfile.TemporaryFile() as archive, tempfile.TemporaryFile() as errors:
        if shutil.disk_usage('/tmp').free < expected_bytes+SUPPORT_DISK_HEADROOM:
            raise ValueError('support download would consume the reserved disk headroom')
        # The official UI proxies this native download. Use authenticated
        # HTTP streaming, avoiding the bulk kubectl exec WebSocket transport.
        if not re.fullmatch(r'/v1/supportbundles/longhorn-node-[0-9]+/[a-z0-9][-a-z0-9.]{0,252}/download', path):
            raise ValueError('support download requires its exact native bundle path')
        uri = '/api/v1/namespaces/'+NS+'/services/http:longhorn-frontend:80/proxy'+path
        argv = [str(lab.BASE/'kube-bin/kubectl'), '--kubeconfig='+str(lab.DIRECTORY/'kubectl.kubeconfig'),
            '--request-timeout=330s', 'get', '--raw='+uri]
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=archive, stderr=errors)
        started = time.monotonic()
        try:
            while True:
                try:
                    rc = process.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic()-started >= 330 or os.fstat(archive.fileno()).st_size > expected_bytes:
                        raise ValueError('support private download exceeded its time or byte budget')
                    print('LONGHORN_SUPPORT_DOWNLOAD_HEARTBEAT bytes='+str(os.fstat(archive.fileno()).st_size), flush=True)
            actual = os.fstat(archive.fileno()).st_size
            errors.seek(0); error_digest = hashlib.file_digest(errors, 'sha256').hexdigest(); errors.seek(0)
            tail = errors.read(16384).decode(errors='replace')
            result = {'rc': rc, 'expected_bytes': expected_bytes, 'received_bytes': actual,
                'stderr_sha256': error_digest, 'elapsed_s': time.monotonic()-started,
                'curl_codes': sorted(set(int(v) for v in re.findall(r'curl: \((\d+)\)', tail))),
                'unexpected_eof': 'unexpected EOF' in tail, 'timeout': 'timed out' in tail.lower()}
            if rc or actual != expected_bytes:
                # Preserve the first native error privately; a digest alone
                # cannot distinguish exec transport from archive generation.
                with tempfile.NamedTemporaryFile(prefix='longhorn-support-download-',
                        suffix='.stderr', dir='/var/tmp', delete=False) as diagnostic:
                    errors.seek(0)
                    shutil.copyfileobj(errors, diagnostic, length=65536)
                    result['private_diagnostic'] = diagnostic.name
            print('LONGHORN_SUPPORT_DOWNLOAD_RESULT '+json.dumps(result, sort_keys=True), flush=True)
            if rc or actual != expected_bytes:
                raise ValueError('support private download failed or declared and received sizes differ')
            archive.seek(0)
            return support_archive(archive)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=10)


def persist(lab, journal):
    lab.write_private(lab.DIRECTORY/RECORD, journal)


class Probe:
    DATA_ENGINE = 'v2'

    def __init__(self, lab, pid, store, configuration, *, monitoring=None, observe=True):
        self.lab = lab; self.pid = pid; self.store = store; self.configuration = configuration
        self.monitoring = monitoring or load('longhorn-v2-monitoring.py')
        self.monitor = self.monitoring.receipt(lab) if observe else None
        self.prom = self.monitoring.Prometheus(lab, self.monitor) if observe else None
        self.journal = None; self.process = None; self.directory = None

    def emit(self, marker, row):
        print(marker+' '+json.dumps(row, sort_keys=True), flush=True)

    def create(self, obj):
        return self.lab.create_business_object(self.journal, self.lab.DIRECTORY/RECORD, obj)

    def volume(self):
        volume = self.lab.get('volumes.longhorn.io', self.journal['volume'], NS)
        if not volume or volume['metadata']['uid'] != self.journal['volume_uid'] or volume['spec'].get('dataEngine') != self.DATA_ENGINE:
            raise ValueError('observability source volume identity changed')
        return volume

    def engine(self):
        volume = self.volume()
        engines = [e for e in self.lab.get('engines.longhorn.io', namespace=NS)['items']
                   if e['spec'].get('volumeName') == self.journal['volume']]
        if (len(engines) != 1 or engines[0]['spec'].get('dataEngine') != self.DATA_ENGINE or
                engines[0].get('status', {}).get('currentState') != 'running' or
                engines[0]['spec'].get('nodeID') != volume['status'].get('currentNodeID') or
                not any(r.get('uid') == volume['metadata']['uid'] and r.get('kind') == 'Volume'
                        for r in engines[0]['metadata'].get('ownerReferences', []))):
            raise ValueError('observability source engine ownership/readiness differs')
        return engines[0]

    def owned_consumers(self):
        self.volume()
        self.monitoring.namespace_graph(self.lab, self.monitor)
        expected = {self.journal['volume']: self.journal['volume_uid']}
        for name, receipt in self.monitor['claims'].items():
            claim = self.lab.get('pvc', name, self.monitor['namespace'])
            pv = self.lab.get('pv', receipt['pv'])
            if (not claim or claim['metadata']['uid'] != receipt['uid'] or
                    not pv or pv['metadata']['uid'] != receipt['pv_uid'] or
                    pv['spec']['claimRef']['uid'] != receipt['uid']):
                raise ValueError('monitoring volume consumer identity changed')
            volume = self.lab.get('volumes.longhorn.io', pv['spec']['csi']['volumeHandle'], NS)
            if not volume or volume['spec'].get('dataEngine') != self.DATA_ENGINE or volume['spec'].get('numberOfReplicas') != 1:
                raise ValueError('monitoring fixture consumer scope changed')
            expected[volume['metadata']['name']] = volume['metadata']['uid']
        actual = {v['metadata']['name']: v['metadata']['uid'] for v in self.lab.get('volumes.longhorn.io', namespace=NS)['items']}
        if actual != expected:
            raise ValueError('observability source fault refuses foreign or additional volume consumers')
        return expected

    def metric(self, name, **labels):
        labels = dict(labels, volume=self.journal['volume'])
        expression = name+'{'+','.join(k+'='+json.dumps(v) for k, v in sorted(labels.items()))+'}'
        return self.prom.query(expression)

    def rules(self):
        token = self.lab.owner()['run_id'].replace('-', '')[:16]
        volume = json.dumps(self.journal['volume']); self.prefix = 'KubeautoLonghorn'+token
        def actual_size():
            self.volume()
            return self.prom.query('longhorn_volume_actual_size_bytes{volume='+volume+'}', allow_empty=True)
        actual = self.lab.wait('owned-volume-actual-size-scraped', 180, actual_size)
        threshold = max(s['value'] for s in actual)+FILLER_BYTES//2
        probe = 'kubeauto_'+token
        expressions = {'Degraded': 'longhorn_volume_robustness{volume='+volume+',state="degraded"} == 1',
            'Space': 'longhorn_volume_actual_size_bytes{volume='+volume+'} > '+str(int(threshold)),
            'Backup': 'longhorn_volume_last_backup_at{volume='+volume+'} == 0',
            'Missing': 'absent_over_time(longhorn_volume_capacity_bytes{volume='+volume+',kubeauto_longhorn_probe='+json.dumps(probe)+'}[45s])'}
        rule = {'apiVersion': 'monitoring.coreos.com/v1', 'kind': 'PrometheusRule',
            'metadata': {'name': 'observability-rules', 'namespace': self.journal['namespace'], 'labels': {'release': 'prometheus'}},
            'spec': {'groups': [{'name': 'kubeauto-owned-longhorn', 'interval': '10s', 'rules': [
                {'alert': self.prefix+suffix, 'expr': expr, 'for': '20s',
                 'labels': {'severity': 'warning', 'kubeauto_run': token}}
                for suffix, expr in expressions.items()]}]}}
        monitor = {'apiVersion': 'monitoring.coreos.com/v1', 'kind': 'ServiceMonitor',
            'metadata': {'name': 'observability-missing-samples', 'namespace': self.journal['namespace'],
                         'labels': {'release': 'prometheus'}},
            'spec': {'selector': {'matchLabels': {'app': 'longhorn-manager'}},
                'namespaceSelector': {'matchNames': [NS]}, 'endpoints': [{'port': 'manager', 'interval': '10s',
                    'scrapeTimeout': '5s', 'relabelings': [{'targetLabel': 'kubeauto_longhorn_probe', 'replacement': probe}]}]}}
        obj = self.create(monitor)
        self.journal[KEY].update(sample_monitor={'name': obj['metadata']['name'], 'uid': obj['metadata']['uid'],
            'original_spec': obj['spec'], 'expected_spec': obj['spec']}, metric_probe=probe, space_threshold_bytes=threshold,
            alert_prefix=self.prefix)
        persist(self.lab, self.journal)
        self.lab.wait('owned-probe-real-samples', 180, lambda: self.prom.query(
            'longhorn_volume_capacity_bytes{volume='+volume+',kubeauto_longhorn_probe='+json.dumps(probe)+'}', allow_empty=True))
        self.create(rule)
        self.alert('Degraded', False); self.alert('Space', False); self.alert('Missing', False)
        self.alert('Backup', True)
        self.emit('LONGHORN_V2_REAL_PROMETHEUS_RULES', {'volume_uid': self.journal['volume_uid'],
            'source': 'Real Prometheus API, 1.12.1 robustness state labels; official old scalar examples are not used.',
            'bounded_space_threshold_bytes': threshold, 'sample_probe': probe, 'alert_prefix': self.prefix})

    def alert(self, suffix, active):
        result = self.lab.wait('real-alert-'+suffix+'-'+str(active).lower(), 240,
                               lambda: self.prom.alert(self.prefix+suffix, active))
        row = {'alert': self.prefix+suffix, 'active': active, 'volume_uid': self.journal['volume_uid'],
               'observed_at': time.time()}
        self.journal[KEY].setdefault('alerts', []).append(row); persist(self.lab, self.journal)
        self.emit('LONGHORN_V2_REAL_ALERT', row)
        return result

    def logs(self):
        return [json.loads(s) for s in (self.directory/'ledger.log').read_text().splitlines()
                if s.startswith('{') and s.endswith('}')]

    def ack_count(self):
        if self.process.poll() is not None:
            complete = [r for r in self.logs() if r.get('type') == 'complete']
            if self.process.returncode or not complete:
                raise ValueError('confirmed-load writer exited without normal completion')
        return len([r for r in self.logs() if r.get('type') == 'ack'])

    def start(self):
        self.directory = Path(tempfile.mkdtemp(prefix='kubeauto-v2-long-load-', dir='/var/tmp'))
        os.chmod(self.directory, 0o700)
        token = self.lab.owner()['run_id']+':observability-30min'
        self.journal[KEY].update(token=token, log_directory=str(self.directory))
        persist(self.lab, self.journal)
        self.stream = (self.directory/'ledger.log').open('w')
        self.process = subprocess.Popen([str(self.lab.BASE/'kube-bin/kubectl'),
            '--kubeconfig='+str(self.lab.DIRECTORY/'kubectl.kubeconfig'), '-n', self.journal['namespace'],
            'exec', 'writer', '--', 'python3', '-u', '-c', sustained_program(token)],
            stdin=subprocess.DEVNULL, stdout=self.stream, stderr=self.stream)
        self.lab.wait('sustained-fsync-acks-started', 90, lambda: self.ack_count() >= 4)
        self.journal[KEY]['writer_start'] = next(r for r in self.logs() if r.get('type') == 'started')
        persist(self.lab, self.journal)

    def network_fault(self):
        self.owned_consumers()
        if not self.lab.healthy_replicas(self.journal['volume']):
            raise ValueError('replica transport fault requires three healthy source replicas')
        engine = self.engine(); source_node = engine['spec']['nodeID']
        addresses = {'longhorn-node-'+a.rsplit('.', 1)[1]: a for a in self.lab.WORKERS}
        remote = sorted([r for r in self.lab.get('replicas.longhorn.io', namespace=NS)['items']
            if r['spec'].get('volumeName') == self.journal['volume'] and r['spec'].get('nodeID') != source_node],
            key=lambda r: r['metadata']['name'])
        if len(remote) != 2 or source_node not in addresses:
            raise ValueError('source transport topology is not the three owned nodes')
        replica = remote[0]; name = replica['metadata']['name']
        if (replica['spec'].get('dataEngine') != 'v2' or replica.get('status', {}).get('currentState') != 'running' or
                not any(r.get('uid') == self.journal['volume_uid'] and r.get('kind') == 'Volume'
                        for r in replica['metadata'].get('ownerReferences', []))):
            raise ValueError('source remote replica is not owned by the observed V2 volume')
        endpoint = engine.get('status', {}).get('currentReplicaAddressMap', {}).get(name, '')
        parsed = urlsplit(endpoint if '://' in endpoint else 'tcp://'+endpoint)
        if (parsed.scheme != 'tcp' or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.port is None or parsed.port != replica.get('status', {}).get('port') or
                not 1024 <= parsed.port <= 65535):
            raise ValueError('source native replica TCP endpoint differs from its owned data port')
        fault = load('longhorn-v2-runtime-faults.py')
        # The isolated monitoring volumes share these IMs. Bind only this
        # volume's connection; Pod addresses are independent of SSH addresses.
        source_manager = engine.get('status', {}).get('instanceManagerName')
        destination_manager = replica.get('status', {}).get('instanceManagerName')
        runtime = {'client_node': source_node, 'managers': {}, 'instances': {
            'engines.longhorn.io': {engine['metadata']['name']: {'manager': source_manager}}}}
        for manager, node in ((source_manager, source_node), (destination_manager, replica['spec']['nodeID'])):
            obj = self.lab.get('instancemanagers.longhorn.io', manager, NS) if manager else None
            pod = self.lab.get('pod', manager, NS) if obj else None
            if (not obj or not pod or obj['spec'].get('nodeID') != node or
                    obj.get('status', {}).get('currentState') != 'running'):
                raise ValueError('source data connection has no original running IM namespace')
            runtime['managers'][manager] = {'uid': obj['metadata']['uid'],
                'spec': obj['spec'], 'pod_uid': pod['metadata']['uid']}
        endpoints = fault.network_endpoints(self.lab, runtime, engine['metadata']['name'], engine, replica)
        if parsed.hostname != endpoints['destination']['ip']:
            raise ValueError('source native replica address differs from its original IM Pod IP')
        sockets = self.lab.kubectl('-n', NS, 'exec', source_manager, '-c', 'instance-manager', '--',
            'cat', '/proc/net/tcp', timeout=30).stdout
        if not fault.established_socket(sockets, endpoints['source']['ip'], endpoints['destination']['ip'], parsed.port):
            raise ValueError('source native replica lacks its actual established data connection')
        address = addresses[source_node]
        proof = {'address': address, 'source': endpoints['source']['ip'], 'destination': parsed.hostname, 'port': parsed.port,
            'host_network': endpoints['source']['host_network'], 'endpoints': endpoints, 'runtime': runtime,
            'engine_name': engine['metadata']['name'], 'manager': destination_manager,
            'comment': 'kubeauto-longhorn-v2-'+hashlib.sha256((self.lab.owner()['run_id']+self.journal['volume_uid']+':53').encode()).hexdigest()[:24],
            'original_host': fault.probe(self.lab, address), 'engine': engine['metadata']['name'],
            'engine_uid': engine['metadata']['uid'], 'replica': name, 'replica_uid': replica['metadata']['uid'],
            'before_acks': self.ack_count(), 'intent': True,
            'prior_events': [e['metadata']['uid'] for e in self.lab.get('events', namespace=NS)['items']]}
        def request(operation):
            return fault.rule_request(proof, operation)
        if json.loads(fault.host_call(self.lab, address, request('probe')).stdout)['present'] != [False, False]:
            raise ValueError('source transport fault comment already exists')
        self.journal[KEY]['network'] = proof; persist(self.lab, self.journal)
        result = json.loads(fault.host_call(self.lab, address, request('add')).stdout)
        if result['present'] != [True, True]:
            raise ValueError('source replica transport fault did not install both bounded rules')
        def degraded():
            current = self.engine()
            if current['metadata']['uid'] != proof['engine_uid']:
                raise ValueError('source engine changed during its endpoint fault')
            # Engine and Volume are reconciled separately. The ERR entry may
            # already be removed when Volume reports degraded; bind its native
            # transition event instead of requiring simultaneous CR snapshots.
            failed = current.get('status', {}).get('replicaModeMap', {}).get(name) == 'ERR'
            if not failed:
                failed = any(e['metadata']['uid'] not in proof['prior_events'] and
                    e.get('involvedObject', {}).get('uid') == proof['engine_uid'] and
                    e['involvedObject'].get('kind') == 'Engine' and
                    e.get('reason') == 'Faulted' and e.get('type') == 'Warning' and
                    e.get('message') == f'Detected replica {name} ({parsed.hostname}:{parsed.port}) in error'
                    for e in self.lab.get('events', namespace=NS)['items'])
            return failed and self.volume().get('status', {}).get('robustness') == 'degraded'
        self.lab.wait('observability-native-volume-degraded', 180, degraded)
        self.alert('Degraded', True)
        self.restore_network()
        self.lab.wait('observability-three-source-replicas-restored', 900,
                      lambda: self.lab.healthy_replicas(self.journal['volume']))
        self.alert('Degraded', False)
        count = self.ack_count()
        self.lab.wait('fault-load-post-recovery-fsync-acks', 90, lambda: self.ack_count() >= count+4)
        proof['after_acks'] = self.ack_count(); proof['native_degraded_and_restored'] = True
        persist(self.lab, self.journal)
        self.emit('LONGHORN_V2_SUSTAINED_FAULT', {k: proof[k] for k in
            ('engine_uid', 'replica_uid', 'before_acks', 'after_acks', 'native_degraded_and_restored')})

    def restore_network(self):
        proof = self.journal.get(KEY, {}).get('network')
        if not proof or not proof.get('intent'):
            return
        fault = load('longhorn-v2-runtime-faults.py')
        fault.validate_transport(proof)
        comment = 'kubeauto-longhorn-v2-'+hashlib.sha256((self.lab.owner()['run_id']+self.journal['volume_uid']+':53').encode()).hexdigest()[:24]
        if (proof['address'] != fault.address(self.lab, proof['runtime']['client_node']) or proof['comment'] != comment):
            raise ValueError('observability transport recovery differs from the original owned intent')
        row = json.loads(fault.host_call(self.lab, proof['address'], fault.rule_request(proof, 'remove')).stdout)
        if row['present'] != [False, False]:
            raise ValueError('owned replica transport fault is not fully removed')
        proof['restored'] = True; persist(self.lab, self.journal)

    def sample_monitor(self, missing):
        proof = self.journal[KEY]['sample_monitor']
        obj = self.lab.get('servicemonitor', proof['name'], self.journal['namespace'])
        if (not obj or obj['metadata']['uid'] != proof['uid'] or
                obj['spec'] not in (proof['original_spec'], proof['expected_spec'])):
            raise ValueError('missing-sample fixture ServiceMonitor changed outside its owner')
        desired = json.loads(json.dumps(proof['original_spec']))
        if missing:
            desired['selector']['matchLabels'] = {'kubeauto.invalid/missing': self.journal[KEY]['metric_probe']}
        proof['expected_spec'] = desired; persist(self.lab, self.journal)
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': proof['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
                 {'op': 'replace', 'path': '/spec', 'value': desired}]
        self.lab.kubectl('-n', self.journal['namespace'], 'patch', 'servicemonitor', proof['name'],
            '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))

    def missing(self):
        self.sample_monitor(True)
        self.alert('Missing', True)
        # The product's own scrape remains healthy: only the separately
        # labelled fixture has no samples. An empty response is never zero.
        self.metric('longhorn_volume_capacity_bytes')
        self.sample_monitor(False)
        self.alert('Missing', False)

    def retire_rules(self):
        # A continued 30-minute ledger can later exceed the deliberately low
        # pressure threshold. Retire this completed fixture rule after proving
        # recovery, so normal continued writes do not create a second incident.
        uid = self.journal['objects'].get('PrometheusRule/observability-rules')
        if not uid:
            return
        obj = self.lab.get('prometheusrule', 'observability-rules', self.journal['namespace'])
        if obj:
            if obj['metadata']['uid'] != uid:
                raise ValueError('owned alert fixture replaced before retirement')
            self.lab.remove(obj)
            self.lab.wait('completed-observability-rules-retired', 90,
                lambda: self.lab.get('prometheusrule', 'observability-rules', self.journal['namespace']) is None)

    def pressure(self):
        native = load('longhorn-trim.py')
        before = self.disk_used()
        code = ('import hashlib,json,os,stat\nfrom pathlib import Path\nSIZE='+str(FILLER_BYTES)+'\n'+
                inspect.getsource(native.filler)+'\nprint(json.dumps(filler("/data/pressure-filler","create"),sort_keys=True))')
        identity = json.loads(self.lab.kubectl('-n', self.journal['namespace'], 'exec', 'writer', '--',
            'python3', '-c', code, timeout=300).stdout)
        self.journal[KEY]['pressure_filler'] = identity; persist(self.lab, self.journal)
        def allocated():
            current = self.disk_used()
            return current if all(current[k]-v >= FILLER_BYTES//2 for k, v in before.items()) else None
        peak = self.lab.wait('bounded-real-raw-disk-allocation', 180, allocated)
        self.alert('Space', True)
        remove = ('import hashlib,json,os,stat\nfrom pathlib import Path\nSIZE='+str(FILLER_BYTES)+'\n'+
                  inspect.getsource(native.filler)+'\nprint(json.dumps(filler("/data/pressure-filler","remove",%r),sort_keys=True))' % identity)
        self.lab.kubectl('-n', self.journal['namespace'], 'exec', 'writer', '--', 'python3', '-c', remove, timeout=300)
        # Reuse the native manager diagnostic action with this source-only
        # engine guard. It neither inspects nor trims monitoring volumes.
        probe = native.Probe(self.lab, self.journal, self.lab.replica_topology(self.journal, 3))
        probe.engine = self.engine
        probe.action('trimFilesystem')
        def reclaimed():
            current = self.disk_used()
            return current if all(peak[k]-v >= FILLER_BYTES//2 for k, v in current.items()) else None
        after = self.lab.wait('bounded-real-raw-disk-capacity-reclaimed', 600, reclaimed)
        self.alert('Space', False)
        self.emit('LONGHORN_V2_BOUNDED_CAPACITY', {'filler_bytes': FILLER_BYTES,
            'before': before, 'peak': peak, 'after': after, 'filler_sha256': identity['sha256'],
            'boundary': '512MiB fsync workload and native lvstore allocation; fixture alert threshold, not a full-disk or hypervisor allocation test.'})

    def disk_used(self):
        result = {}
        for node in self.lab.get('nodes.longhorn.io', namespace=NS)['items']:
            entries = node.get('status', {}).get('diskStatus', {})
            if len(entries) != 1:
                raise ValueError('capacity observation requires the original single raw disk per node')
            disk = next(iter(entries.values()))
            maximum = disk.get('storageMaximum'); available = disk.get('storageAvailable')
            if (type(maximum) is not int or type(available) is not int or maximum <= 0 or
                    not 4*1024**3 <= available <= maximum):
                raise ValueError('raw-disk capacity is invalid or below its four-GiB stop budget')
            result[node['metadata']['name']] = maximum-available
        if len(result) != 3:
            raise ValueError('capacity observation lost an owned raw-disk node')
        return result

    def backup(self):
        name = 'observability-backup-'+secrets.token_hex(8); snapshot = 'observability-snapshot-'+secrets.token_hex(8)
        request = {'kind': 'snapshot', 'name': snapshot, 'volume': self.journal['volume']}
        backup = {'kind': 'backup', 'name': name, 'snapshot': snapshot, 'mode': 'full'}
        self.journal.setdefault('native_snapshots', {})[snapshot] = {'request': request}
        self.journal.setdefault('backups', {})[name] = {'request': backup}
        persist(self.lab, self.journal)
        self.lab.run_requests(self.pid, [request, backup], backup_configuration=self.configuration)
        self.lab.bind_snapshots(self.journal); self.lab.bind_backups(self.journal)
        obj = self.lab.get('backups.longhorn.io', name, NS)
        if not obj or obj.get('status', {}).get('state') != 'Completed' or obj['status'].get('error'):
            raise ValueError('backup freshness alert cannot resolve without a completed product backup')
        receipt = self.store.backup_receipt(self.journal['volume'], name, 2*1024**3)
        self.journal['backups'][name]['object_receipt'] = receipt; persist(self.lab, self.journal)
        self.alert('Backup', False)
        self.emit('LONGHORN_V2_BACKUP_ALERT_RECOVERED', {'backup_uid': obj['metadata']['uid'],
            'volume_uid': self.journal['volume_uid'], 'object_receipt_sha256': hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
            'boundary': 'Freshness/no-success alert activation followed by actual full backup; not an injected backend failure alert.'})

    def profile(self):
        runtime = load('longhorn-v2-runtime-resources.py')
        result = {}
        for manager in self.lab.get('instancemanagers.longhorn.io', namespace=NS)['items']:
            name = manager['metadata']['name']; pod = self.lab.get('pod', name, NS)
            if (manager['spec'].get('dataEngine') != 'v2' or manager['spec'].get('type') != 'aio' or
                    not load('longhorn-v2-control-faults.py').pod_ready(pod) or
                    not any(r.get('uid') == manager['metadata']['uid'] and r.get('controller')
                            for r in pod['metadata'].get('ownerReferences', []))):
                raise ValueError('resource observation requires the owned healthy V2 AIO managers')
            row = json.loads(self.lab.kubectl('-n', NS, 'exec', name, '-c', 'instance-manager', '--',
                'python3', '-c', runtime.runtime_script(), timeout=60).stdout)
            stats = json.loads(self.lab.kubectl('-n', NS, 'exec', name, '-c', 'instance-manager', '--',
                'python3', '-c', rpc_program(), timeout=60).stdout)
            if any(row['memory_events'].get(k, 0) for k in ('oom', 'oom_kill')):
                raise ValueError('SPDK has a real cgroup OOM event')
            options = stats['framework_get_config']
            if (len(options) != 1 or options[0].get('method') != 'iobuf_set_options' or
                    options[0].get('params', {}).get('small_bufsize') != 8192 or
                    options[0]['params'].get('large_bufsize') != 132*1024):
                raise ValueError('native iobuf allocation does not match the pinned SPDK implementation')
            current = self.lab.get('pod', name, NS)
            if not current or current['metadata']['uid'] != pod['metadata']['uid']:
                raise ValueError('resource observation mixed different SPDK Pod identities')
            result[name] = {'pod_uid': pod['metadata']['uid'], 'manager_uid': manager['metadata']['uid'],
                'node': pod['spec']['nodeName'], 'runtime': row, 'iobuf': stats}
        if len(result) != 3:
            raise ValueError('resource observation lost an owned V2 SPDK instance manager')
        return result

    def queue(self):
        for size, depth in ((4096, 1), (4096, 8), (4096, 32), (128*1024, 1), (128*1024, 8), (128*1024, 32)):
            before = self.profile()
            code = ('import hashlib,json,math,os,threading,time\nfrom pathlib import Path\n'
                    'from concurrent.futures import ThreadPoolExecutor\n'+inspect.getsource(queue_workload)+
                    '\nprint(json.dumps(queue_workload("/data",%r,%d,%d),sort_keys=True))' %
                    (self.journal[KEY]['token'], size, depth))
            row = json.loads(self.lab.kubectl('-n', self.journal['namespace'], 'exec', 'writer', '--',
                'python3', '-c', code, timeout=300).stdout)
            after = self.profile(); deltas = {}
            if set(before) != set(after):
                raise ValueError('SPDK manager set changed during queued load')
            for name, first in before.items():
                last = after[name]
                if (first['pod_uid'] != last['pod_uid'] or
                    any(first['runtime'][k] != last['runtime'][k] for k in ('pid', 'start_time', 'argv')) or
                    first['iobuf']['framework_get_config'] != last['iobuf']['framework_get_config']):
                    raise ValueError('queued load crossed a changed SPDK process or allocation')
                deltas[name] = iobuf_delta(first['iobuf']['iobuf_get_stats'], last['iobuf']['iobuf_get_stats'])
            retries = sum(values['retry'] for pools in deltas.values() for modules in pools.values() for values in modules.values())
            self.emit('LONGHORN_V2_IOBUF_WORKLOAD', {'workload': row, 'iobuf_counter_deltas': deltas,
                'allocation': {n: v['iobuf']['framework_get_config'] for n, v in before.items()},
                'resource_before': {n: v['runtime'] for n, v in before.items()},
                'resource_after': {n: v['runtime'] for n, v in after.items()},
                'exhaustion_retry_observed': retries > 0, 'tuning_benefit_claimed': False,
                'boundary': 'Includes the six owned monitoring volumes as measured background traffic. '
                    'Retry deltas are observed under this size/depth, not attributed exclusively to the foreground client. '
                    'No tuning comparison or production exhaustion threshold is claimed.'})

    def attach(self):
        lab = self.lab; journal = self.journal; namespace = journal['namespace']
        start = time.monotonic(); volumes = {}; digests = {}; rows = []
        for index in range(4):
            name = 'concurrent-'+str(index)
            self.create({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': name, 'namespace': namespace},
                'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': 'longhorn-delete',
                         'resources': {'requests': {'storage': '512Mi'}}}})
        def backlog(stage):
            samples = []
            for name, original in volumes.items():
                volume = lab.get('volumes.longhorn.io', original['name'], NS)
                if not volume or volume['metadata']['uid'] != original['uid']:
                    raise ValueError('bounded attach queue volume identity changed')
                state = volume.get('status', {}).get('state', '')
                samples.append({'claim': name, 'volume_uid': original['uid'], 'state': state})
            rows.append({'stage': stage, 'elapsed_s': time.monotonic()-start, 'volumes': samples,
                         'backlog': sum(s['state'] in ('attaching', 'detaching') for s in samples)})
            self.monitoring.host_memory(lab); self.disk_used()
        for index in range(4):
            name = 'concurrent-'+str(index)
            claim = lab.wait('bounded-concurrent-pvc-'+name, 600, lambda:
                (p if p and p.get('status', {}).get('phase') == 'Bound' else None)
                if (p := lab.get('pvc', name, namespace)) else None)
            lab.bind_business_volume(journal, lab.DIRECTORY/RECORD, name)
            pv = lab.get('pv', claim['spec']['volumeName'])
            volume = lab.get('volumes.longhorn.io', pv['spec']['csi']['volumeHandle'], NS)
            if (not volume or volume['spec'].get('dataEngine') != 'v2' or
                    volume['spec'].get('numberOfReplicas') != 3 or pv['spec']['claimRef']['uid'] != claim['metadata']['uid']):
                raise ValueError('bounded attach workload did not consume the three-replica V2 product class')
            volumes[name] = {'name': volume['metadata']['name'], 'uid': volume['metadata']['uid']}
        for generation in range(2):
            names = []
            for index in range(4):
                claim = 'concurrent-'+str(index); name = claim+'-client-'+str(generation); names.append(name)
                node = 'longhorn-node-'+lab.WORKERS[(index+generation) % 3].rsplit('.', 1)[1]
                self.create(lab.client_pod(namespace, name, claim, node))
            def ready():
                backlog('attach-'+str(generation))
                return all(load('longhorn-v2-control-faults.py').pod_ready(lab.get('pod', name, namespace)) for name in names)
            lab.wait('four-concurrent-product-volume-attaches-'+str(generation), 600, ready)
            for index, name in enumerate(names):
                payload = lab.owner()['run_id']+':bounded-attach:'+str(index)
                actual = lab.kubectl('-n', namespace, 'exec', name, '--', 'python3', '-c',
                    lab.payload_program(payload, read_only=generation == 1)).stdout.strip()
                expected = hashlib.sha256(payload.encode()*262144).hexdigest()
                if actual != expected:
                    raise ValueError('bounded concurrent attach lost confirmed application bytes')
                digests['concurrent-'+str(index)] = expected
            for name in names:
                obj = lab.get('pod', name, namespace)
                if not obj or obj['metadata']['uid'] != journal['objects']['Pod/'+name]:
                    raise ValueError('concurrent attach client was replaced before fencing')
                lab.remove(obj)
            def detached():
                backlog('detach-'+str(generation))
                return all(lab.get('pod', name, namespace) is None for name in names) and all(
                    lab.get('volumes.longhorn.io', v['name'], NS).get('status', {}).get('state') == 'detached' for v in volumes.values())
            lab.wait('four-concurrent-product-volume-detaches-'+str(generation), 600, detached)
        self.emit('LONGHORN_V2_BOUNDED_ATTACH_QUEUE', {'volumes': volumes, 'confirmed_sha256': digests,
            'samples': rows, 'elapsed_s': time.monotonic()-start, 'max_concurrent': 4, 'mount_generations': 2,
            'boundary': 'Four 512MiB three-replica PVCs, two placements with fsync/readback; not the upstream 1000-volume attach scale or a production latency guarantee.'})

    def run(self, journal, payload, digest, original_performance):
        self.journal = journal
        if KEY in journal:
            raise ValueError('prior observability workload requires its owned recovery')
        journal[KEY] = {'run_id': self.lab.owner()['run_id'], 'volume_uid': journal['volume_uid']}
        persist(self.lab, journal)
        self.rules(); self.start()
        self.network_fault(); self.pressure(); self.backup(); self.missing(); self.retire_rules()
        original_performance(journal, payload, digest)
        self.queue(); self.attach(); self.support_bundle()
        last = time.monotonic()
        while self.process.poll() is None:
            now = time.monotonic()
            if now-last >= 15:
                count = self.ack_count(); self.monitoring.host_memory(self.lab); self.disk_used()
                self.emit('LONGHORN_V2_SUSTAINED_HEARTBEAT', {'confirmed': count,
                    'remaining_s': max(0, LOAD_SECONDS-self.logs()[-1].get('observed_elapsed_s', 0))})
                last = now
            time.sleep(1)
        if self.process.returncode:
            raise ValueError('thirty-minute writer did not exit normally')
        proof = confirmed_load(self.logs(), journal[KEY]['token'])
        load('longhorn-faults.py').verify_client_ledger(self.lab, journal, 'writer', proof)
        network = journal[KEY]['network']
        if (not network.get('native_degraded_and_restored') or not network.get('restored') or
                network['after_acks'] <= network['before_acks']):
            raise ValueError('sustained load lacks its native fault/recovery and acknowledgements')
        self.emit('LONGHORN_V2_SUSTAINED_RESULT', {k: proof[k] for k in
            ('summary', 'timeline', 'errors', 'retries', 'max_latency_s')})
        self.emit('LONGHORN_V2_SUSTAINED_ACKNOWLEDGEMENTS',
                  {'token': journal[KEY]['token'], 'rows': self.logs()})
        journal[KEY]['confirmed'] = proof; persist(self.lab, journal)
        # The accepted business wrapper moves this exact source to another
        # worker after return. Its payload verifier is extended below to
        # verify the complete long-load acknowledgement prefix there as well.

    def manager_nodes(self):
        return self.lab.NODES

    def manager(self):
        ds = self.lab.get('daemonset', 'longhorn-manager', NS)
        pods = [p for p in self.lab.get('pods', namespace=NS)['items']
                if p['metadata'].get('labels', {}).get('app') == 'longhorn-manager' and
                load('longhorn-v2-control-faults.py').pod_ready(p) and
                any(r.get('uid') == ds['metadata']['uid'] and r.get('controller')
                    for r in p['metadata'].get('ownerReferences', []))]
        expected = {'longhorn-node-'+a.rsplit('.', 1)[1] for a in self.manager_nodes()}
        if len(pods) != len(expected) or {p['spec']['nodeName'] for p in pods} != expected:
            raise ValueError('support bundle requires all owned product managers')
        pod = sorted(pods, key=lambda p: p['metadata']['name'])[0]
        address = str(ipaddress.IPv4Address(pod['status']['podIP']))
        return pod, 'http://'+address+':9500'

    def support_bundle(self):
        if self.lab.get('supportbundles.longhorn.io', namespace=NS)['items']:
            raise ValueError('support bundle fixture refuses pre-existing diagnostics')
        pod, endpoint = self.manager()
        description = 'kubeauto-observability-'+self.lab.owner()['run_id']
        self.journal[KEY]['support_bundle'] = {'description': description, 'create_intent': True}
        persist(self.lab, self.journal)
        self.lab.kubectl('-n', NS, 'exec', '-i', pod['metadata']['name'], '-c', 'longhorn-manager', '--',
            'curl', '--fail', '--silent', '--show-error', '--max-time', '30', '--request', 'POST',
            '--header', 'Content-Type: application/json', '--data-binary', '@-', endpoint+'/v1/supportbundles',
            stdin=json.dumps({'issueURL': '', 'description': description}), timeout=45)
        def owned():
            rows = [s for s in self.lab.get('supportbundles.longhorn.io', namespace=NS)['items']
                    if s['spec'].get('description') == description and not s['spec'].get('issueURL')]
            if len(rows) > 1:
                raise ValueError('support bundle create outcome is ambiguous')
            return rows[0] if rows else None
        obj = self.lab.wait('owned-native-support-bundle-created', 60, owned)
        proof = self.journal[KEY]['support_bundle']
        proof.update(name=obj['metadata']['name'], uid=obj['metadata']['uid']); persist(self.lab, self.journal)
        def ready():
            current = owned()
            if not current or current['metadata']['uid'] != proof['uid']:
                raise ValueError('support bundle ownership changed during generation')
            state = current.get('status', {}).get('state')
            if state in ('Error', 'Replaced', 'Unknown'):
                raise ValueError('support bundle generation entered a failed state')
            return current if state == 'ReadyForDownload' else None
        obj = self.lab.wait('owned-native-support-bundle-ready', 600, ready)
        self.bundle_dependents(proof)
        status = obj['status']; owner = status.get('ownerID')
        metadata = {'source_uid': proof['uid'], 'name': proof['name'], 'owner': owner,
                    'compressed_bytes': status.get('filesize'), 'state': status.get('state')}
        proof['ready_metadata'] = metadata; persist(self.lab, self.journal)
        self.emit('LONGHORN_V2_SUPPORT_BUNDLE_METADATA', metadata)
        if (owner not in {'longhorn-node-'+a.rsplit('.', 1)[1] for a in self.lab.NODES} or
                not 0 < status.get('filesize', 0) <= SUPPORT_COMPRESSED_BYTES or
                not re.fullmatch(r'[a-z0-9][-a-z0-9.]{0,252}', proof['name'])):
            raise ValueError('support bundle owner or private download size is invalid')
        path = '/v1/supportbundles/'+owner+'/'+proof['name']+'/download'
        result = download_support(self.lab, path, status['filesize'])
        self.lab.wait('support-bundle-native-download-retired', 600,
                      lambda: self.lab.get('supportbundles.longhorn.io', proof['name'], NS) is None)
        self.lab.wait('support-bundle-native-dependents-retired', 600,
                      lambda: not self.bundle_dependents(proof))
        proof['retired_after_download'] = True; persist(self.lab, self.journal)
        self.emit('LONGHORN_V2_SUPPORT_BUNDLE_BOUNDARY', dict(result, source_uid=proof['uid']))

    def bundle_dependents(self, proof):
        objects = [obj for kind in ('deployments', 'daemonsets', 'replicasets', 'jobs', 'pods')
                   for obj in self.lab.get(kind, namespace=NS)['items']]
        known = set(proof.get('dependent_uids', [])) | {proof['uid']}
        while True:
            added = {obj['metadata']['uid'] for obj in objects
                     if any(r.get('uid') in known for r in obj['metadata'].get('ownerReferences', []))} - known
            if not added:
                break
            known |= added
        proof['dependent_uids'] = sorted(known - {proof['uid']})
        persist(self.lab, self.journal)
        return [obj for obj in objects if obj['metadata']['uid'] in known]

    def restore(self):
        if not self.journal:
            return
        if self.journal.get(KEY, {}).get('run_id') != self.lab.owner()['run_id']:
            raise ValueError('observability cleanup belongs to another run')
        self.restore_network()
        if self.journal[KEY].get('sample_monitor'):
            self.sample_monitor(False)
        self.retire_rules()
        bundle = self.journal[KEY].get('support_bundle')
        if bundle:
            rows = [s for s in self.lab.get('supportbundles.longhorn.io', namespace=NS)['items']
                    if s['spec'].get('description') == bundle['description']]
            if len(rows) > 1:
                raise ValueError('support bundle recovery cannot select an ambiguous create outcome')
            if rows:
                obj = rows[0]
                if bundle.get('uid') and obj['metadata']['uid'] != bundle['uid']:
                    raise ValueError('support bundle replaced before cleanup')
                if obj['spec'].get('issueURL'):
                    raise ValueError('support bundle changed outside its diagnostic owner')
                bundle['uid'] = obj['metadata']['uid']
                self.bundle_dependents(bundle)
                self.lab.remove(obj)
                self.lab.wait('owned-support-bundle-clean', 300,
                    lambda: self.lab.get('supportbundles.longhorn.io', obj['metadata']['name'], NS) is None)
            if bundle.get('uid'):
                self.lab.wait('owned-support-bundle-dependents-clean', 600,
                              lambda: not self.bundle_dependents(bundle))
        # Removing the original application Pod in business cleanup fences a
        # blocked writer at its owning mount. Never leave an exec child after
        # simply terminating the local kubectl connection.
        if self.journal[KEY].get('token'):
            pod = self.lab.get('pod', 'writer', self.journal['namespace'])
            if pod and pod['metadata']['uid'] != self.journal['objects'].get('Pod/writer'):
                raise ValueError('long-load writer Pod replaced before fencing')
            if pod:
                self.lab.remove(pod)
                self.lab.wait('long-load-original-writer-fenced', 180,
                              lambda: self.lab.get('pod', 'writer', self.journal['namespace']) is None)
        if self.process is not None and self.process.poll() is None:
            try: self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.terminate(); self.process.wait(timeout=10)
        if self.directory:
            print('LONGHORN_PRIVATE_DIAGNOSTIC directory='+str(self.directory), flush=True)


def business(lab, pid):
    monitoring = load('longhorn-v2-monitoring.py')
    monitoring.verify(lab, pid)
    store = lab.backup_store()
    with lab.scoped_cleanup(store.cleanup):
        store.prepare(); store.qualify()
        configuration = store.product_configuration()
        probe = Probe(lab, pid, store, configuration)
        original_performance = lab.verify_performance
        original_cleanup = lab.cleanup_business
        original_kubectl = lab.kubectl
        original_client_pod = lab.client_pod
        def performance(journal, payload, digest):
            probe.run(journal, payload, digest, original_performance)
        def cleanup(record=None, **kwargs):
            probe.restore()
            return original_cleanup(record, **kwargs)
        def kubectl(*args, **kwargs):
            result = original_kubectl(*args, **kwargs)
            if ('exec' in args and 'remount-reader' in args and 'python3' in args and probe.journal and
                    probe.journal.get(KEY, {}).get('confirmed') and any(
                        isinstance(a, str) and "'/data/confirmed'" in a for a in args)):
                faults = load('longhorn-faults.py')
                # Use the original transport to avoid recursive interception.
                from types import SimpleNamespace
                faults.verify_client_ledger(SimpleNamespace(kubectl=original_kubectl), probe.journal,
                    'remount-reader', probe.journal[KEY]['confirmed'])
                probe.emit('LONGHORN_V2_SUSTAINED_REMOUNT', probe.journal[KEY]['confirmed']['summary'])
            return result
        lab.verify_performance = performance; lab.cleanup_business = cleanup; lab.kubectl = kubectl
        lab.client_pod = lambda *args, **kwargs: client_pod(original_client_pod, *args, **kwargs)
        try:
            lab.business('performance', relay_pid=pid, storage_class='longhorn-delete')
        finally:
            lab.verify_performance = original_performance; lab.cleanup_business = original_cleanup; lab.kubectl = original_kubectl
            lab.client_pod = original_client_pod
            if probe.process is not None and probe.process.poll() is None:
                # Scoped cleanup already fenced its exact Pod or refused; a
                # local connection can now be stopped without broad kills.
                probe.process.terminate()
                try: probe.process.wait(timeout=10)
                except subprocess.TimeoutExpired: probe.process.kill(); probe.process.wait(timeout=10)
            if hasattr(probe, 'stream'): probe.stream.close()
    print('LONGHORN_V2_OBSERVABILITY_BUSINESS_PASS', flush=True)


def cleanup(lab, pid):
    """Restore exact shared faults before deleting this module's consumers."""
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('observability refuses a symlink recovery journal')
    if path.exists():
        journal = json.loads(path.read_text())
        proof = journal.get(KEY)
        if (not isinstance(proof, dict) or proof.get('run_id') != lab.owner()['run_id'] or
                proof.get('volume_uid') != journal.get('volume_uid')):
            raise ValueError('observability cannot recover another business journal')
        lab.check_business_graph(journal)
        probe = Probe(lab, pid, lab.backup_store(), None)
        probe.journal = journal
        probe.restore()
        lab.cleanup_business(relay_pid=pid)
    lab.backup_store().cleanup()
    # The monitoring stack is an explicitly retained, separately journaled
    # baseline. Its objects are not cleanup authority for this business owner.
    monitoring = load('longhorn-v2-monitoring.py')
    monitor_path = lab.DIRECTORY/monitoring.RECORD
    if monitor_path.exists() or monitor_path.is_symlink():
        monitoring.verify(lab, pid)
    if path.exists():
        raise ValueError('observability business journal remains after cleanup')
    print('LONGHORN_V2_OBSERVABILITY_BUSINESS_SCOPED_CLEAN_PASS', flush=True)


def verify_log(log):
    """Validate native results, raw fsync acknowledgements and exact scope."""
    lines = log.splitlines()
    if re.search(r'^(LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', log, re.M):
        raise ValueError('observability log contains a failure marker')
    def rows(marker):
        return [json.loads(line[len(marker)+1:]) for line in lines if line.startswith(marker+' ')]
    def one(marker):
        values = rows(marker)
        if len(values) != 1:
            raise ValueError('observability requires one complete observation: '+marker)
        return values[0]
    rule = one('LONGHORN_V2_REAL_PROMETHEUS_RULES')
    if not rule.get('volume_uid') or not rule.get('alert_prefix') or rule.get('bounded_space_threshold_bytes', 0) <= 0:
        raise ValueError('observability rule ownership/threshold is incomplete')
    alerts = rows('LONGHORN_V2_REAL_ALERT')
    expected = {'Degraded': [False, True, False], 'Space': [False, True, False],
                'Backup': [True, False], 'Missing': [False, True, False]}
    if (len(alerts) != sum(map(len, expected.values())) or
            any(r.get('volume_uid') != rule['volume_uid'] or
                type(r.get('active')) is not bool or type(r.get('observed_at')) not in (int, float) or
                not math.isfinite(r['observed_at']) for r in alerts)):
        raise ValueError('observability real alert observations are incomplete')
    for suffix, states in expected.items():
        selected = [r for r in alerts if r['alert'] == rule['alert_prefix']+suffix]
        if ([r['active'] for r in selected] != states or
                any(a['observed_at'] > b['observed_at'] for a, b in zip(selected, selected[1:]))):
            raise ValueError('observability alert lacks activation/recovery: '+suffix)
    ledger = one('LONGHORN_V2_SUSTAINED_ACKNOWLEDGEMENTS')
    if not ledger.get('token', '').endswith(':observability-30min'):
        raise ValueError('observability load token scope differs')
    proof = confirmed_load(ledger['rows'], ledger['token'])
    result = one('LONGHORN_V2_SUSTAINED_RESULT')
    if result != {k: proof[k] for k in ('summary', 'timeline', 'errors', 'retries', 'max_latency_s')}:
        raise ValueError('observability sustained result differs from raw acknowledgements')
    if one('LONGHORN_V2_SUSTAINED_REMOUNT') != proof['summary']:
        raise ValueError('observability cross-node remount prefix differs')
    fault = one('LONGHORN_V2_SUSTAINED_FAULT')
    if (not fault.get('engine_uid') or not fault.get('replica_uid') or
            fault.get('native_degraded_and_restored') is not True or
            not 4 <= fault.get('before_acks', -1) < fault.get('after_acks', -1) <= proof['summary']['confirmed']):
        raise ValueError('observability sustained fault/recovery acknowledgements differ')
    capacity = one('LONGHORN_V2_BOUNDED_CAPACITY')
    if (capacity.get('filler_bytes') != FILLER_BYTES or
            not re.fullmatch('[0-9a-f]{64}', capacity.get('filler_sha256', '')) or
            len(capacity.get('before', {})) != 3 or
            set(capacity['before']) != set(capacity.get('peak', {})) or
            set(capacity['before']) != set(capacity.get('after', {})) or
            any(capacity['peak'][k]-v < FILLER_BYTES//2 or
                capacity['peak'][k]-capacity['after'][k] < FILLER_BYTES//2
                for k, v in capacity['before'].items())):
        raise ValueError('observability raw capacity pressure/recovery is incomplete')
    backup = one('LONGHORN_V2_BACKUP_ALERT_RECOVERED')
    if (backup.get('volume_uid') != rule['volume_uid'] or not backup.get('backup_uid') or
            not re.fullmatch('[0-9a-f]{64}', backup.get('object_receipt_sha256', ''))):
        raise ValueError('observability backup alert lacks its full-backup object receipt')
    performance = one('LONGHORN_PERFORMANCE_RESULT')
    if (performance.get('errors') != 0 or performance.get('verified_blocks') != 16384 or
            performance.get('dataset_bytes') != 64*1024**2 or performance.get('write_fsync_per_operation') is not True or
            performance.get('buffered_io') is not True or performance.get('concurrency') != 1 or
            [r.get('mode') for r in performance.get('results', [])] !=
            ['sequential-write', 'random-write', 'sequential-read', 'random-read']):
        raise ValueError('observability sequential/random performance result is incomplete')
    for measured in performance['results']:
        if (measured.get('operations') != (256 if measured['mode'].startswith('sequential') else 4096) or
                set(measured.get('latency_ms', {})) != {'50', '95', '99'} or
                any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
                    for v in measured['latency_ms'].values())):
            raise ValueError('observability performance lacks measured fsync/read percentiles')
    profile = one('LONGHORN_PERFORMANCE_RESOURCE_PROFILE')
    if (not profile.get('before') or set(profile['before']) != set(profile.get('after', {})) or
            any(v.get('uid') != profile['after'][k].get('uid') for k, v in profile['before'].items())):
        raise ValueError('observability measured resource windows differ')
    workloads = rows('LONGHORN_V2_IOBUF_WORKLOAD')
    combinations = {(s, d) for s in (4096, 128*1024) for d in (1, 8, 32)}
    if len(workloads) != 6 or {(r['workload']['size_bytes'], r['workload']['requested_depth']) for r in workloads} != combinations:
        raise ValueError('observability iobuf workload size/depth coverage is incomplete')
    for row in workloads:
        work = row['workload']
        if (work.get('errors') != 0 or work.get('operations') != 128 or work.get('verified_operations') != 128 or
                work.get('fsync_per_write') is not True or
                not 1 <= work.get('measured_peak_inflight', 0) <= work['requested_depth'] or
                set(work.get('latency_ms', {})) != {'50', '95', '99'} or
                any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in work['latency_ms'].values()) or
                row.get('tuning_benefit_claimed') is not False or len(row.get('allocation', {})) != 3 or
                set(row['allocation']) != set(row.get('resource_before', {})) or
                set(row['allocation']) != set(row.get('resource_after', {})) or
                set(row['allocation']) != set(row.get('iobuf_counter_deltas', {}))):
            raise ValueError('observability iobuf observations exceed their measured boundary')
        retries = 0
        for name, deltas in row['iobuf_counter_deltas'].items():
            first, last = row['resource_before'][name], row['resource_after'][name]
            allocation = row['allocation'][name]
            if (len(allocation) != 1 or allocation[0].get('method') != 'iobuf_set_options' or
                    any(type(allocation[0].get('params', {}).get(k)) is not int or allocation[0]['params'][k] <= 0
                        for k in ('large_bufsize', 'small_bufsize', 'large_pool_count', 'small_pool_count')) or
                    allocation[0]['params']['large_bufsize'] != 132*1024 or allocation[0]['params']['small_bufsize'] != 8192):
                raise ValueError('observability lacks actual bounded iobuf pool allocation')
            if (type(first.get('pid')) is not int or not first.get('start_time') or not first.get('argv') or
                    any(first.get(k) != last.get(k) for k in ('pid', 'start_time', 'argv')) or
                    any(first.get('memory_events', {}).get(k, 0) or last.get('memory_events', {}).get(k, 0)
                        for k in ('oom', 'oom_kill'))):
                raise ValueError('observability workload mixed different SPDK processes')
            for pools in deltas.values():
                for pool in ('small_pool', 'large_pool'):
                    if any(type(pools[pool][k]) is not int or pools[pool][k] < 0 for k in ('cache', 'main', 'retry')):
                        raise ValueError('observability iobuf counter deltas are invalid')
                    retries += pools[pool]['retry']
        if row.get('exhaustion_retry_observed') is not (retries > 0):
            raise ValueError('observability iobuf exhaustion claim differs from real retry counters')
    attach = one('LONGHORN_V2_BOUNDED_ATTACH_QUEUE')
    if (attach.get('max_concurrent') != 4 or attach.get('mount_generations') != 2 or
            len(attach.get('volumes', {})) != 4 or
            set(attach['volumes']) != set(attach.get('confirmed_sha256', {})) or
            len({r.get('uid') for r in attach['volumes'].values() if r.get('uid')}) != 4 or
            any(not re.fullmatch('[0-9a-f]{64}', v) for v in attach['confirmed_sha256'].values()) or
            not {'attach-0', 'attach-1', 'detach-0', 'detach-1'} <= {r.get('stage') for r in attach.get('samples', [])} or
            any(not 0 <= r.get('backlog', -1) <= 4 for r in attach['samples'])):
        raise ValueError('observability bounded concurrent volume contract is incomplete')
    bundle = one('LONGHORN_V2_SUPPORT_BUNDLE_BOUNDARY')
    if (not 0 < bundle.get('compressed_bytes', 0) <= SUPPORT_COMPRESSED_BYTES or
            not 0 < bundle.get('members', 0) <= 10000 or not 0 <= bundle.get('expanded_bytes', -1) <= SUPPORT_EXPANDED_BYTES or
            bundle.get('crc_verified') is not True or bundle.get('extracted') is not False or
            bundle.get('published') is not False or not bundle.get('source_uid') or
            not re.fullmatch('[0-9a-f]{64}', bundle.get('sha256', ''))):
        raise ValueError('observability support bundle boundary is incomplete')
    for marker in ('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', 'LONGHORN_V2_OBSERVABILITY_BUSINESS_PASS'):
        if lines.count(marker) != 1:
            raise ValueError('observability terminal/cleanup contract is incomplete: '+marker)
    print('LONGHORN_V2_OBSERVABILITY_LOG_VERIFIED', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'cleanup', 'verify'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log: parser.error('verify requires --log')
        verify_log(args.log.read_text())
        return
    if args.relay_pid <= 0: parser.error('mutation requires a live relay')
    _, lab, _ = load('longhorn-v2-backups.py').fixture()
    lab.require_lease()
    if args.mode == 'business':
        business(lab, args.relay_pid)
    else:
        cleanup(lab, args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Store/API exception bodies may carry credentials. Retain privately.
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-observability-failure-',
                suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_OBSERVABILITY_FAIL private_diagnostic='+diagnostic.name, file=sys.stderr)
        sys.exit(1)
