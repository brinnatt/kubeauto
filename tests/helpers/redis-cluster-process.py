#!/usr/bin/env python3
"""Node-side Redis fault fixture; emits only verified process identity, never CRI environment."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def process_identity(pid, container_id, root=Path('/proc')):
    directory = root / str(pid)
    stat = (directory / 'stat').read_text()
    fields = stat.rsplit(')', 1)[-1].split()
    if (pid <= 1 or not stat.startswith(str(pid) + ' (redis-server)') or
            len(fields) < 20 or not fields[19].isdigit() or
            (directory / 'comm').read_text().strip() != 'redis-server' or
            container_id not in (directory / 'cgroup').read_text()):
        raise ValueError('Redis host process identity mismatch')
    return {'pid': pid, 'pid_start': fields[19], 'state': fields[0]}


def inspect_identity(request):
    identifier = request['container_id']
    if not re.fullmatch(r'[0-9a-f]{64}', identifier):
        raise ValueError('Redis CRI identifier is not exact')
    result = subprocess.run(['crictl', '--runtime-endpoint', 'unix:///run/containerd/containerd.sock',
                             'inspect', '--output', 'json', identifier],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise ValueError('Redis CRI inspect failed')
    data = json.loads(result.stdout)
    status = data['status']
    labels = status['labels']
    required = {'io.kubernetes.pod.uid': request['uid'],
                'io.kubernetes.pod.name': request['pod'],
                'io.kubernetes.pod.namespace': request['namespace'],
                'io.kubernetes.container.name': request['container']}
    if (status['id'] != identifier or status['state'] != 'CONTAINER_RUNNING' or
            any(labels.get(key) != value for key, value in required.items())):
        raise ValueError('Redis CRI ownership/state mismatch')
    pid = data['info']['pid']
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise ValueError('Redis CRI process identifier is invalid')
    return process_identity(pid, identifier)


def act(request):
    action = request['action']
    if action not in ('inspect', 'stop', 'continue'):
        raise ValueError('Unknown Redis process fixture action')
    if action == 'continue':
        # Never signal a replacement process, even when the numeric PID is reused.
        try:
            current = process_identity(request['pid'], request['container_id'])
        except FileNotFoundError:
            return {'outcome': 'original-process-gone'}
        if current['pid_start'] != request['pid_start']:
            return {'outcome': 'original-process-replaced'}
    current = inspect_identity(request)
    if action == 'inspect':
        return current
    if (current['pid'] != request['pid'] or current['pid_start'] != request['pid_start']):
        raise ValueError('Redis process changed before signal')
    os.kill(current['pid'], signal.SIGSTOP if action == 'stop' else signal.SIGCONT)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        verified = process_identity(current['pid'], request['container_id'])
        if verified['pid_start'] != current['pid_start']:
            raise ValueError('Redis process changed after signal')
        if (verified['state'] == 'T') == (action == 'stop'):
            return {**verified, 'outcome': 'paused' if action == 'stop' else 'resumed'}
        time.sleep(0.05)
    raise ValueError('Redis signal did not change actual process state')


def main():
    try:
        print(json.dumps(act(json.load(sys.stdin))), flush=True)
    except Exception as exc:
        # CRI inspect includes Secret-backed environment; never print the raw
        # result or exception body, even on parse failures.
        print(json.dumps({'error': type(exc).__name__}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
