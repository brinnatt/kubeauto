#!/usr/bin/env python3
"""Validate product Exporters with a pinned owned Prometheus scrape/alert fixture."""
import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import yaml

KEY = 'brinnatt/prometheus:v3.13.1-distroless'
LOCAL = 'registry.talkschool.cn:5000/redis-lab/' + KEY


def publication(evidence):
    if (evidence.get('workflow_conclusion') != 'success' or evidence.get('durable_rc') != 0 or
            evidence.get('failure_markers') != 0 or len(evidence.get('images', [])) != 1):
        raise ValueError('observability fixture publication is incomplete')
    record = evidence['images'][0]
    if (record.get('key') != KEY or record.get('head') != evidence.get('head') or
            record.get('talkedu_ref') != 'hub.talkedu.cn/kubeauto/' + KEY.removeprefix('brinnatt/') or
            record.get('dockerhub_ref') != KEY or set(record.get('platforms', {})) != {'linux/amd64', 'linux/arm64'}):
        raise ValueError('observability fixture identity differs from accepted publication')
    return record


def inspect_raw(ref, insecure=False):
    command = ['skopeo', 'inspect', '--raw']
    if insecure:
        command.append('--tls-verify=false')
    result = subprocess.run([*command, 'docker://' + ref], capture_output=True, timeout=75)
    if result.returncode:
        raise ValueError('observability artifact Registry read failed')
    return result.stdout


def artifacts(record):
    raw = inspect_raw(record['talkedu_ref'])
    platforms = {r['platform']['architecture']: r['digest'] for r in json.loads(raw)['manifests']
                 if r.get('platform', {}).get('os') == 'linux'}
    if ('sha256:' + hashlib.sha256(raw).hexdigest() != record['digest'] or
            any(platforms.get(arch) != record['platforms']['linux/' + arch] for arch in ('amd64', 'arm64'))):
        raise ValueError('observability current manifest differs from accepted dual-push artifact')
    print('REDIS_OBSERVABILITY_ARTIFACT_PASS current-talkedu=verified dual-push=accepted-inputs digest=' + record['digest'], flush=True)


def cache(record):
    result = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false', 'docker://' + LOCAL],
                            capture_output=True, timeout=75)
    if result.returncode:
        if b'manifest unknown' not in result.stderr.lower() and b'name unknown' not in result.stderr.lower():
            raise ValueError('observability local Registry prerequisite is unavailable')
        result = subprocess.run(['skopeo', 'copy', '--preserve-digests', '--override-os', 'linux',
                                 '--override-arch', 'amd64', '--dest-tls-verify=false',
                                 'docker://' + record['talkedu_ref'].rsplit(':', 1)[0] + '@' + record['platforms']['linux/amd64'],
                                 'docker://' + LOCAL], capture_output=True, timeout=600)
        if result.returncode:
            raise ValueError('observability fixed artifact cache staging failed')
        raw = inspect_raw(LOCAL, insecure=True)
    else:
        raw = result.stdout
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != record['platforms']['linux/amd64']:
        raise ValueError('observability local cache digest differs from accepted platform')


def fixture_documents(namespace, name, run_id, members, image):
    labels = {'kubeauto.io/component': 'redis-observability-test', 'kubeauto.io/managed-by': 'kubeauto',
              'kubeauto.io/test-run': run_id}
    metadata = {'name': name, 'namespace': namespace, 'labels': labels}
    config = {'global': {'scrape_interval': '2s', 'scrape_timeout': '1s', 'evaluation_interval': '2s'},
              'rule_files': ['/etc/prometheus/rules.yml'], 'scrape_configs': [{
                  'job_name': 'redis-gate-exporters', 'static_configs': [
                      {'targets': [pod['status']['podIP'] + ':9121'],
                       'labels': {'redis_member': pod['metadata']['name']}} for pod in members]}]}
    rules = {'groups': [{'name': 'redis-gate-owned', 'rules': [{
        'alert': 'RedisExporterUnreachable', 'expr': 'up{job="redis-gate-exporters"} == 0', 'for': '6s',
        'labels': {'severity': 'critical'}, 'annotations': {'summary': 'Redis Exporter scrape is unreachable'}}]}]}
    return [{'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata,
             'data': {'prometheus.yml': yaml.safe_dump(config), 'rules.yml': yaml.safe_dump(rules)}},
            {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': metadata, 'spec': {
                'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                'securityContext': {'runAsUser': 65534, 'runAsGroup': 65534, 'fsGroup': 65534},
                'containers': [{'name': 'prometheus', 'image': image,
                    'args': ['--config.file=/etc/prometheus/prometheus.yml', '--storage.tsdb.path=/prometheus',
                             '--storage.tsdb.retention.time=30m', '--storage.tsdb.retention.size=64MB'],
                    'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                                  'limits': {'cpu': '1', 'memory': '512Mi'}},
                    'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                    'volumeMounts': [{'name': 'config', 'mountPath': '/etc/prometheus', 'readOnly': True},
                                     {'name': 'work', 'mountPath': '/prometheus'}],
                    'readinessProbe': {'httpGet': {'path': '/-/ready', 'port': 9090}, 'periodSeconds': 2}}],
                'volumes': [{'name': 'config', 'configMap': {'name': name}},
                            {'name': 'work', 'emptyDir': {'sizeLimit': '128Mi'}}]}}]


def restart_scraper(gate, document):
    """Reconnect the owned scraper after policy change; existing TCP is not a fault."""
    name = document['metadata']['name']
    current = gate.get('pod', name)
    if current['metadata'].get('labels') != document['metadata']['labels']:
        raise ValueError('scraper restart refuses changed fixture ownership')
    gate.k('delete', '--raw', f'/api/v1/namespaces/{gate.ns}/pods/{name}', '-f', '-', input_text=json.dumps({
        'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {
            k: current['metadata'][k] for k in ('uid', 'resourceVersion')}}))
    gate.k('wait', '--for=delete', 'pod/' + name, '-n', gate.ns, '--timeout=120s', timeout=140)
    gate.k('create', '-f', '-', input_text=json.dumps(document))
    gate.k('wait', '--for=condition=Ready', 'pod/' + name, '-n', gate.ns, '--timeout=180s', timeout=200)
    if gate.get('pod', name)['metadata']['uid'] == current['metadata']['uid']:
        raise ValueError('owned scraper was not restarted with fresh network connections')
    print('REDIS_OBSERVABILITY_SCRAPER_RECONNECT_PASS owned-pod=changed product-pods=retained', flush=True)
    return gate.forward(name, 9090, reuse=False)


def lifecycle_events(pod, events):
    """Require current healthy lifecycle; classify only proven startup recovery."""
    metadata = pod['metadata']
    status = pod.get('status', {})
    ready = next((c for c in status.get('conditions', []) if c['type'] == 'Ready'), {})
    containers = status.get('containerStatuses', [])
    if (status.get('phase') != 'Running' or ready.get('status') != 'True' or
            not containers or any(not c.get('ready') or c.get('restartCount') != 0 or
                                  'running' not in c.get('state', {}) for c in containers)):
        raise ValueError('observability scraper is not currently healthy without restarts')
    def timestamp(value):
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError('lifecycle timestamp lacks timezone')
        return result
    ready_time = timestamp(ready['lastTransitionTime'])
    reasons = set()
    recovered = 0
    endpoint = status['podIP'] + ':9090'
    expected_message = (f'Readiness probe failed: Get "http://{endpoint}/-/ready": '
                        f'dial tcp {endpoint}: connect: connection refused')
    for event in events:
        owner = event.get('involvedObject', {})
        # The deliberate reconnect creates a new Pod under the same name.
        if owner.get('uid') != metadata['uid']:
            continue
        if any(owner.get(k) != metadata[k] for k in ('name', 'namespace')) or owner.get('kind') != 'Pod':
            raise ValueError('observability lifecycle event has foreign identity')
        if event.get('type') == 'Normal':
            reasons.add(event.get('reason'))
            continue
        times = [event.get('lastTimestamp'), event.get('eventTime'),
                 event.get('series', {}).get('lastObservedTime')]
        times = [timestamp(value) for value in times if value]
        if (event.get('type') != 'Warning' or event.get('reason') != 'Unhealthy' or
                event.get('message') != expected_message or not times or max(times) > ready_time):
            raise ValueError('observability lifecycle event is unresolved or unclassified')
        recovered += 1
    if not {'Scheduled', 'Started'} <= reasons:
        raise ValueError('observability target lacks current Kubernetes lifecycle events')
    return recovered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('artifacts', 'observability'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    raw_evidence = os.environ.get('REDIS_OBSERVABILITY_PUBLICATION_EVIDENCE')
    evidence = (json.loads(raw_evidence) if raw_evidence else json.loads(
        (args.base / 'tests/evidence/redis-observability-images-20261013.json').read_text()))
    record = publication(evidence)
    artifacts(record)
    if args.module == 'artifacts':
        return
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    spec = importlib.util.spec_from_file_location('redis_observability_focused', args.base / 'tests/helpers/redis-focused-gate.py')
    focused = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(focused)
    with tempfile.TemporaryDirectory(prefix='redis-observability-') as temporary, ExitStack() as stack:
        gate = focused.Gate(args, Path(temporary), stack)
        gate.baseline()
        cache(record)
        name = 'redis-prometheus-' + gate.run_id[:12]
        members = gate.members()['replication']
        documents = fixture_documents(gate.ns, name, gate.run_id, members,
                                      LOCAL + '@' + record['platforms']['linux/amd64'])
        intended = []
        failure = None
        try:
            _, client = gate.seeded_primary()
            for doc in documents:
                intended.append(doc['kind'].lower())
                gate.k('create', '-f', '-', input_text=json.dumps(doc))
            gate.k('wait', '--for=condition=Ready', 'pod/' + name, '-n', gate.ns, '--timeout=180s', timeout=200)
            port = gate.forward(name, 9090)
            def query(expression):
                url = 'http://127.0.0.1:' + str(port) + '/api/v1/query?' + urllib.parse.urlencode({'query': expression})
                with urllib.request.urlopen(url, timeout=10) as response:
                    answer = json.load(response)
                if answer.get('status') != 'success' or answer['data']['resultType'] != 'vector':
                    raise ValueError('Prometheus query did not produce a valid instant vector')
                return answer['data']['result']
            def wait_for(action, stage):
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if action():
                        return
                    print('REDIS_OBSERVABILITY_HEARTBEAT stage=' + stage, flush=True)
                    time.sleep(3)
                raise ValueError('observability bounded wait failed: ' + stage)
            def all_up():
                rows = query('redis_up{job="redis-gate-exporters"}')
                return (len(rows) == 3 and {r['metric']['redis_member'] for r in rows} ==
                        {p['metadata']['name'] for p in members} and all(float(r['value'][1]) == 1 for r in rows))
            wait_for(all_up, 'three-exporters-scraped')
            for metric in ('redis_memory_used_bytes', 'redis_connected_clients', 'redis_commands_processed_total'):
                if len(query(metric + '{job="redis-gate-exporters"}')) != 3:
                    raise ValueError('product Exporter lacks required scraped Redis metric')
            replica = next(p['metadata']['name'] for p in members if p['metadata']['name'] != gate.primary())
            alert = 'ALERTS{alertname="RedisExporterUnreachable",alertstate="firing",redis_member="' + replica + '"}'
            with gate.isolated([replica]):
                gate.disconnect_existing(replica)
                # Prometheus uses persistent scrape connections. Kubernetes
                # explicitly leaves policy effects on existing TCP undefined.
                # Restart only our ephemeral scraper to prove new denied TCP.
                port = restart_scraper(gate, documents[1])
                def exact_scrape_fault():
                    rows = query('up{job="redis-gate-exporters"}')
                    return len(rows) == 3 and all(float(r['value'][1]) ==
                        (0 if r['metric']['redis_member'] == replica else 1) for r in rows)
                try:
                    wait_for(exact_scrape_fault, 'exact-one-exporter-scrape-denied')
                    wait_for(lambda: len(query(alert)) == 1, 'owned-exporter-alert-firing')
                except BaseException:
                    try:
                        print('REDIS_OBSERVABILITY_FIRST_SCRAPE_STATE ' + json.dumps({
                            'up': query('up{job="redis-gate-exporters"}'),
                            'alerts': query('ALERTS{alertname="RedisExporterUnreachable"}')}), flush=True)
                    except Exception:
                        print('REDIS_OBSERVABILITY_SCRAPE_DIAGNOSTICS_FAIL first failure retained', flush=True)
                    raise
                if client.command('GET', gate.key) != gate.value:
                    raise ValueError('observability fault lost the retained business digest')
                print('REDIS_OBSERVABILITY_ALERT_PASS member=' + replica + ' state=firing', flush=True)
            wait_for(all_up, 'exporters-recovered')
            wait_for(lambda: not query(alert), 'alert-cleared')
            events = json.loads(gate.k('get', 'events', '-n', gate.ns,
                '--field-selector', 'involvedObject.name=' + name, '-o', 'json'))['items']
            current = gate.get('pod', name)
            if current['metadata'].get('labels') != documents[1]['metadata']['labels']:
                raise ValueError('observability lifecycle target ownership changed')
            recovered = lifecycle_events(current, events)
            print('REDIS_OBSERVABILITY_EVENTS_PASS current-uid=' + current['metadata']['uid'] +
                  ' recovered-startup-warnings=' + str(recovered) + ' unresolved-warnings=0', flush=True)
            print('REDIS_OBSERVABILITY_PASS redis-up=three-members metrics=memory,clients,commands '
                  'alert=fired-and-cleared events=verified business-digest=retained', flush=True)
        except BaseException as exc:
            failure = exc
            try:
                gate.fault_diagnostics()
                print(gate.k('logs', name, '-n', gate.ns, '--tail=40'), flush=True)
            except Exception:
                print('REDIS_OBSERVABILITY_DIAGNOSTICS_FAIL first failure retained', flush=True)
            raise
        finally:
            cleanup_failure = None
            for kind in reversed(intended):
                try:
                    doc = json.loads(gate.k('get', kind, name, '-n', gate.ns, '--ignore-not-found', '-o', 'json') or '{}')
                    if not doc:
                        continue
                    if doc['metadata'].get('labels', {}) != documents[0]['metadata']['labels']:
                        raise ValueError('observability cleanup refuses changed fixture ownership')
                    gate.k('delete', '--raw', f'/api/v1/namespaces/{gate.ns}/' +
                        ('pods/' if kind == 'pod' else 'configmaps/') + name, '-f', '-', input_text=json.dumps({
                            'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {
                                'uid': doc['metadata']['uid'], 'resourceVersion': doc['metadata']['resourceVersion']}}))
                    gate.k('wait', '--for=delete', kind + '/' + name, '-n', gate.ns, '--timeout=120s', timeout=140)
                except Exception as exc:
                    cleanup_failure = exc
            try:
                gate.cleanup_keys()
            except Exception as exc:
                cleanup_failure = exc
            if cleanup_failure is not None:
                if failure is None:
                    raise cleanup_failure
                print('REDIS_OBSERVABILITY_CLEANUP_FAIL first failure retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=observability', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=observability', flush=True)


if __name__ == '__main__':
    main()
