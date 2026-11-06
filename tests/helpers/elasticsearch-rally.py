#!/usr/bin/env python3
"""Run the frozen offline track; emit numeric report fields only."""
import csv
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys


def report(path):
    values = {}
    with path.open() as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['Metric', 'Task', 'Value', 'Unit']:
            raise ValueError('Rally report schema differs from 2.13.0')
        for row in reader:
            if row['Task'] not in ('bulk', 'search') or not row['Value']:
                continue
            number = float(row['Value'])
            if not math.isfinite(number) or number < 0: raise ValueError('invalid Rally measurement')
            values.setdefault(row['Task'], {})[row['Metric']] = {'value': number, 'unit': row['Unit']}
    for task in ('bulk', 'search'):
        if task not in values or values[task].get('error rate', {}).get('value') != 0:
            raise ValueError('Rally task is missing or has request errors')
        if values[task].get('Median Throughput', {}).get('value', 0) <= 0:
            raise ValueError('Rally task has no positive throughput')
    # The 10k bulk corpus yields fewer samples than the 4k search operations;
    # do not fabricate unsupported bulk percentile values.
    for metric in ('50th percentile latency', '99th percentile latency'):
        if metric not in values['search']: raise ValueError('search percentile sample evidence is missing')
    return values


def main():
    work = Path('/work')
    os.umask(0o077)
    options = {'default': {'use_ssl': True, 'verify_certs': True, 'ca_certs': '/ca/ca.crt',
               'basic_auth_user': Path('/identity/username').read_text().strip(),
               'basic_auth_password': Path('/identity/password').read_text().strip(), 'timeout': 60, 'max_retries': 0}}
    path = work/'client-options.json'
    with path.open('x') as stream: json.dump(options, stream)
    version = subprocess.run(['esrally', '--version'], capture_output=True, text=True, timeout=30)
    if version.returncode or '2.13.0' not in version.stdout: raise ValueError('Rally runtime version differs')
    # Rally expands the packaged bz2 and creates offset files next to it.
    # Keep the immutable image read-only and use the private emptyDir copy.
    shutil.copytree('/rally/track', work/'track')
    checked = subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=work/'track', capture_output=True, timeout=30)
    if checked.returncode: raise ValueError('offline corpus or track checksum differs')
    command = ['esrally', 'race', '--pipeline=benchmark-only', '--track-path=/work/track', '--offline',
               '--target-hosts=' + os.environ['ELASTICSEARCH_URL'], '--client-options=' + str(path),
               '--report-format=csv', '--show-in-report=all-percentiles', '--report-file=/work/report.csv', '--quiet']
    try:
        with (work/'rally-private.log').open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1500)
        if result.returncode: raise ValueError('Rally race failed; private Pod work directory retains its diagnostic output')
        result = report(work/'report.csv')
        print('ELASTICSEARCH_RALLY_MEASUREMENT ' + json.dumps({'version': '2.13.0', 'track': 'deterministic-catalog-v1', 'documents': 10000,
              'metrics': result, 'p95': 'not emitted by upstream Rally; independent client soak reports its own P95'}, sort_keys=True), flush=True)
        print('ELASTICSEARCH_RALLY_RACE_PASS', flush=True)
    finally:
        path.unlink(missing_ok=True)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('ELASTICSEARCH_RALLY_FAIL reason=' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
