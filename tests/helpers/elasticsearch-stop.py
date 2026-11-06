#!/usr/bin/env python3
"""Stop a frozen full composition after its current durable modules finish.

Only the scheduler is paused. Module processes keep their leases, normal
completion and cleanup. Keep durable wrappers alive to record the real exit.
"""
import json
import os
from pathlib import Path
import re
import signal
import time


BASE = Path('/usr/local/kubeauto')
PREFIX = Path('/tmp/kubeauto-elasticsearch-gate')
HISTORY = Path('/var/tmp/kubeauto-elasticsearch-history')


def process(pid):
    path = Path('/proc') / str(pid)
    try:
        stat = (path/'stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] in ('Z', 'X'):
            return None
        return {'ppid': int(stat[1]), 'start': stat[19],
                'args': (path/'cmdline').read_bytes().decode().rstrip('\0').split('\0')}
    except FileNotFoundError:
        return None


def send(pid, identity, action):
    current = process(pid)
    if not current or current['start'] != identity['start'] or current['args'] != identity['args']:
        raise ValueError('stop refuses a missing or replaced process')
    os.kill(pid, action)


def finish_module(stem):
    log = stem.with_suffix('.log')
    if any(Path(str(stem)+suffix).read_text().strip() != '0' for suffix in ('.exit', '.finalized')):
        raise ValueError('current module failed; let the original supervisor preserve its failure')
    terminal = ('ELASTICSEARCH_DR_PLATFORM_PASS independent_kubernetes=true'
                if stem.name == 'dr-bootstrap' else 'ELASTICSEARCH_MODULE_PASS module='+stem.name)
    raw = log.read_text()
    if terminal not in raw.splitlines() or any(re.match(
            r'^(?:ELASTICSEARCH_(?:FIRST_FAILURE|[A-Z_]*FAIL)\b|Traceback|\[FAIL\])', line) for line in raw.splitlines()):
        raise ValueError('current module lacks its scoped success boundary')


def stop():
    wrapper = int(Path(str(PREFIX)+'.pid').read_text().strip())
    if Path(str(PREFIX)+'.exit').exists():
        raise ValueError('full gate already has a durable exit')
    outer = process(wrapper)
    if not outer or str(BASE/'tests/helpers/run-durable-gate.sh') not in outer['args'] or str(PREFIX) not in outer['args']:
        raise ValueError('stop refuses an unproven durable wrapper')
    candidates = []
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        pid = int(path.name); current = process(pid)
        if not current or str(BASE/'tests/helpers/elasticsearch-full.py') not in current['args']:
            continue
        parent = process(current['ppid'])
        if parent and parent['ppid'] == wrapper and parent['args'][-1] == 'only':
            candidates.append((pid, current))
    if len(candidates) != 1:
        raise ValueError('stop requires exactly one full composition under the owned wrapper')
    pid, identity = candidates[0]
    environment = dict(item.split('=', 1) for item in (Path('/proc')/str(pid)/'environ').read_bytes().decode().split('\0') if '=' in item)
    run = environment.get('ELASTICSEARCH_RUN_ID', '')
    if not re.fullmatch('[a-f0-9]{32}', run):
        raise ValueError('stop requires the actual full-run identity')
    directory = HISTORY/('full-'+run)
    if not directory.is_dir():
        raise ValueError('stop requires existing durable module history')
    send(pid, identity, signal.SIGSTOP)
    terminated = False
    try:
        stems = []
        identities = {}
        for path in directory.glob('*.pid'):
            child = int(path.read_text().strip())
            current = process(child)
            if current:
                if current['ppid'] != pid or str(path.with_suffix('')) not in current['args']:
                    raise ValueError('current module PID belongs to another process')
                stems.append(path.with_suffix(''))
                identities[child] = current
        if not stems:
            raise ValueError('no active module boundary; resume normal supervision')
        print('ELASTICSEARCH_STOP_REQUEST reason=boundary-hardening current='+','.join(s.name for s in stems), flush=True)
        deadline = time.monotonic()+28800; heartbeat = 0
        while True:
            pending = []
            for stem in stems:
                child = int(Path(str(stem)+'.pid').read_text().strip())
                current = process(child)
                if current:
                    if current['start'] != identities[child]['start'] or current['args'] != identities[child]['args']:
                        raise ValueError('current module PID was replaced')
                    pending.append(stem)
                else:
                    finish_module(stem)
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise ValueError('current module exceeded its configured full-run timeout')
            if time.monotonic() >= heartbeat:
                print('ELASTICSEARCH_STOP_WAIT current='+','.join(s.name for s in pending)+
                      ' log_bytes='+str(sum(s.with_suffix('.log').stat().st_size for s in pending)), flush=True)
                heartbeat = time.monotonic()+30
            time.sleep(1)
        # The payload exits with the real signal status. Do not kill its gate
        # shell or durable wrapper: their diagnostics/context restore must run.
        send(pid, identity, signal.SIGTERM)
        send(pid, identity, signal.SIGCONT)
        terminated = True
        print('ELASTICSEARCH_STOP_BOUNDARY_REACHED modules='+','.join(s.name for s in stems)+
              ' source_baseline=retained parent_success=false', flush=True)
    finally:
        current = process(pid)
        if not terminated and current and current['start'] == identity['start']:
            send(pid, identity, signal.SIGCONT)


if __name__ == '__main__':
    try:
        stop()
    except (ValueError, OSError, KeyError) as error:
        print('ELASTICSEARCH_STOP_REFUSED reason='+str(error), flush=True)
        raise SystemExit(1)
