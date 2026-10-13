#!/usr/bin/env python3
"""Explicit Redis CRD schema upgrade with dry-run, ownership and durable backup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

GROUP = 'redis.redis.opstreelabs.in'
NAMES = {name + '.' + GROUP for name in ('redis', 'redisclusters', 'redisreplications', 'redissentinels')}
MANAGER = 'kubeauto-redis-crds'


def definitions(document):
    rows = document.get('items', []) if document.get('kind') == 'List' else [document]
    if (len(rows) != 4 or {r['metadata']['name'] for r in rows} != NAMES or
            any(r['kind'] != 'CustomResourceDefinition' or r['spec']['group'] != GROUP or
                r['spec']['scope'] != 'Namespaced' for r in rows)):
        raise ValueError('Redis CRD bundle differs from the four pinned official definitions')
    return rows


def printed_definitions(text):
    # kubectl prints each visited YAML document separately; a JSON List input
    # may also be visited as individual objects. Never assume one JSON root.
    decoder, rows = json.JSONDecoder(), []
    remaining = text.lstrip()
    while remaining:
        document, end = decoder.raw_decode(remaining)
        if not isinstance(document, dict):
            raise ValueError('Redis CRD printer output contains a non-object')
        rows.extend(document.get('items', []) if document.get('kind') == 'List' else [document])
        remaining = remaining[end:].lstrip()
    return definitions({'kind': 'List', 'items': rows})


def validate_existing(target, current, consumers, namespace):
    if current['metadata']['name'] != target['metadata']['name']:
        raise ValueError('Redis CRD identity changed')
    for field in ('group', 'names', 'scope'):
        # The API server fills optional names fields (listKind, singular).
        expected = target['spec'][field]
        actual = current['spec'][field]
        if isinstance(expected, dict):
            if any(actual.get(k) != v for k, v in expected.items()):
                raise ValueError('Redis CRD API identity differs from the official bundle')
        elif actual != expected:
            raise ValueError('Redis CRD API identity differs from the official bundle')
    versions = {v['name'] for v in target['spec']['versions'] if v.get('served')}
    if not set(current.get('status', {}).get('storedVersions', [])) <= versions:
        raise ValueError('Redis CRD upgrade would remove a stored API version')
    if current['spec'].get('conversion', {}).get('strategy', 'None') != 'None':
        raise ValueError('Redis CRD upgrade refuses an external conversion controller')
    owner = current['metadata'].get('annotations', {}).get('kubeauto.io/redis-owner-namespace')
    if owner is not None and owner != namespace:
        raise ValueError('Redis CRD belongs to another data namespace')
    labels = current['metadata'].get('labels', {})
    if any(labels.get(k) not in (None, v) for k, v in
           {'kubeauto.io/component': 'redis', 'kubeauto.io/managed-by': 'kubeauto'}.items()):
        raise ValueError('Redis CRD has foreign ownership labels')
    for row in consumers:
        metadata = row['metadata']
        if (metadata.get('namespace') != namespace or
                metadata.get('labels', {}).get('kubeauto.io/component') != 'redis' or
                metadata.get('labels', {}).get('kubeauto.io/managed-by') != 'kubeauto'):
            raise ValueError('Redis CRD upgrade refuses a shared or foreign consumer')


def execute(argv, *, stdin=None, timeout=90):
    result = subprocess.run(argv, input=stdin, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Redis CRD command failed: ' + argv[0] + '; ' + result.stderr.strip())
    return result.stdout


def upgrade(args):
    chart_bytes = args.chart.read_bytes()
    fields = args.chart_sha256_file.read_text().split()
    if (len(fields) != 2 or not re.fullmatch(r'[0-9a-f]{64}', fields[0]) or
            fields[1].lstrip('*') != args.chart.name):
        raise ValueError('Redis CRD Chart checksum manifest has a different artifact identity')
    chart_sha256 = fields[0]
    if hashlib.sha256(chart_bytes).hexdigest() != chart_sha256:
        raise ValueError('Redis CRD upgrade Chart checksum mismatch')
    k = ['kubectl', '--kubeconfig=' + str(args.kubeconfig)]
    raw = execute([str(args.helm), 'show', 'crds', str(args.chart)])
    target = printed_definitions(execute([*k, 'create', '--dry-run=client', '--validate=false',
                                          '-f', '-', '-o', 'json'], stdin=raw))
    prior = []
    for row in target:
        name = row['metadata']['name']
        current = json.loads(execute([*k, 'get', 'crd', name, '-o', 'json']))
        consumers = json.loads(execute([*k, 'get', row['spec']['names']['plural'] + '.' + GROUP,
                                       '--all-namespaces', '-o', 'json']))['items']
        validate_existing(row, current, consumers, args.namespace)
        prior.append(current)
        row['metadata']['uid'] = current['metadata']['uid']
        row['metadata']['resourceVersion'] = current['metadata']['resourceVersion']
        row['metadata'].setdefault('annotations', {}).update({
            'kubeauto.io/redis-owner-namespace': args.namespace,
            'kubeauto.io/redis-crd-chart-sha256': chart_sha256})
    body = json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': target})
    apply = [*k, 'apply', '--server-side', '--field-manager=' + MANAGER, '-f', '-', '-o', 'json']
    # Validate every definition before creating a backup or changing any schema.
    planned = printed_definitions(execute([*apply, '--dry-run=server'], stdin=body))
    by_name = {r['metadata']['name']: r for r in planned}
    changed = any(p['spec'] != by_name[p['metadata']['name']]['spec'] or
                  any(p['metadata'].get('annotations', {}).get(key) != value for key, value in
                      by_name[p['metadata']['name']]['metadata'].get('annotations', {}).items()
                      if key.startswith('kubeauto.io/')) for p in prior)
    if changed:
        backup = args.backup_parent / ('crd-' + uuid.uuid4().hex)
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        path = backup / 'before.json'
        with path.open('x') as stream:
            path.chmod(0o600)
            json.dump({'apiVersion': 'v1', 'kind': 'List', 'items': prior}, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        print('REDIS_CRD_BACKUP_SAVED directory=' + str(backup), flush=True)
        printed_definitions(execute(apply, stdin=body))
    for row in target:
        name = row['metadata']['name']
        execute([*k, 'wait', '--for=condition=Established', 'crd/' + name, '--timeout=120s'], timeout=135)
        actual = json.loads(execute([*k, 'get', 'crd', name, '-o', 'json']))
        if actual['spec'] != by_name[name]['spec']:
            raise ValueError('Redis CRD applied schema does not match its server dry-run')
    print('REDIS_CRD_UPGRADE_PASS definitions=4 stored-versions=retained shared-consumers=absent '
          'changed=' + str(changed).lower(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chart', type=Path, required=True)
    parser.add_argument('--chart-sha256-file', type=Path, required=True)
    parser.add_argument('--helm', type=Path, required=True)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--backup-parent', type=Path, required=True)
    upgrade(parser.parse_args())


if __name__ == '__main__':
    main()
