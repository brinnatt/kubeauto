#!/usr/bin/env python3
"""One published Manager pin transition; historical eligibility is not adoption.

The two reverse hashes prove that historical evidence differs only in the
Manager image selection. The owning linked gate separately reconciles setup09,
checks real Pod digests, and exercises the changed eviction behavior.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

BASE = Path('/usr/local/kubeauto')
PLAN = 'tests/fixtures/longhorn-manager-pin-transition.json'
OLD = 'v1.12.1'
NEW = 'v1.12.1-kubeauto.1'
CATALOG = 'roles/longhorn/files/images.json'
CONSTANTS = 'common/constants.py'
NS = 'longhorn-system'


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Manager transition requires a regular non-symlink input')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name):
    spec = importlib.util.spec_from_file_location('manager_adopt_'+name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if hasattr(module, 'BASE'): module.BASE = BASE
    return module


def plan():
    digest(BASE/PLAN)
    row = json.loads((BASE/PLAN).read_text())
    if (row.get('scope') != 'manager-linked-eviction-pin-only' or
            row.get('old_tag') != OLD or row.get('new_tag') != NEW or
            set(row.get('inputs', {})) != {CATALOG, CONSTANTS}):
        raise ValueError('Manager transition exceeds its two product pin inputs')
    ref = row['publication']
    if (ref['path'] != 'tests/evidence/longhorn-manager-eviction-publication-20261024.json' or
            digest(BASE/ref['path']) != ref['sha256']):
        raise ValueError('Manager transition publication bytes changed')
    publication = json.loads((BASE/ref['path']).read_text())
    record = publication.get('publication_record', {})
    if (publication.get('status') != 'pass' or publication.get('durable_rc') != 0 or
            publication.get('failure_markers') != 0 or publication.get('selected_image_count') != 1 or
            publication.get('image') != 'brinnatt/longhorn-manager:'+NEW or
            record.get('key') != publication['image'] or
            set(record.get('platforms', {})) != {'linux/amd64', 'linux/arm64'} or
            any(not re.fullmatch(r'sha256:[0-9a-f]{64}', value or '') for value in
                (record.get('digest'), *record.get('platforms', {}).values()))):
        raise ValueError('Manager transition requires its completed single-image dual publication')
    return row, record


def input_current(name, original):
    if name not in (CATALOG, CONSTANTS): return False
    row, _ = plan()
    edge = row['inputs'][name]
    if edge['old_sha256'] != original or digest(BASE/name) != edge['sha256']: return False
    source = (BASE/name).read_text()
    if name == CATALOG:
        current = '"longhorn-manager": "'+NEW+'"'
        previous = '"longhorn-manager": "'+OLD+'"'
    else:
        current = '            tags["longhorn-manager"] = "'+NEW+'"\n'
        previous = ''
    return (source.count(current) == 1 and
            hashlib.sha256(source.replace(current, previous, 1).encode()).hexdigest() == original)


def equivalent_runtime_manifest(lab, record, image_id):
    """Bind a Docker/OCI representation change to the actual running digest."""
    actual_digest = image_id.rsplit('@', 1)[-1]
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', actual_digest):
        return False
    production, _ = lab.publication_records()
    rows = [row for row in production if row['key'] == 'brinnatt/longhorn-manager:'+NEW]
    if len(rows) != 1 or rows[0]['platforms'] != record['platforms'] or rows[0]['digest'] != record['digest']:
        return False
    row = rows[0]
    expected = lab.published_manifest(row, record['platforms']['linux/amd64'])
    # Fetch by the Pod's immutable digest, never infer runtime identity from a tag.
    response = lab.execute(['skopeo', 'inspect', '--raw', '--tls-verify=false',
        'docker://127.0.0.1:5000/brinnatt/longhorn-manager@'+actual_digest], timeout=60)
    return ('sha256:'+hashlib.sha256(response.stdout.encode()).hexdigest() == actual_digest and
            lab.equivalent_manifest(json.loads(response.stdout), expected))


def runtime(lab, record, *, old=False):
    config = json.loads((lab.DIRECTORY/'yml/longhorn/input.json').read_text())
    image = config['longhorn_image_prefix']+'/longhorn-manager:'+(OLD if old else NEW)
    targets = {'longhorn-node-'+a.rsplit('.', 1)[1] for a in lab.WORKERS}
    rows = {}
    verified = set()
    ds = lab.get('daemonset', 'longhorn-manager', NS)
    if not ds: raise ValueError('Manager DaemonSet is absent')
    for pod in lab.get('pods', namespace=NS)['items']:
        containers = [c for c in pod['spec']['containers'] if c['name'] == 'longhorn-manager']
        if not containers: continue
        node = pod['spec'].get('nodeName')
        states = [s for s in pod.get('status', {}).get('containerStatuses', []) if s['name'] == 'longhorn-manager']
        if (pod['metadata'].get('deletionTimestamp') or node not in targets or node in rows or
                len(containers) != 1 or containers[0]['image'] != image or len(states) != 1 or
                not states[0].get('ready') or not states[0].get('state', {}).get('running') or
                not any(r.get('controller') and r.get('kind') == 'DaemonSet' and
                    r.get('uid') == ds['metadata']['uid'] for r in pod['metadata'].get('ownerReferences', []))):
            raise ValueError('Manager Pods do not match the exact healthy owned DaemonSet')
        image_id = states[0].get('imageID', '')
        if not old and image_id.rsplit('@', 1)[-1] not in (record['digest'], *record['platforms'].values()):
            if image_id not in verified and not equivalent_runtime_manifest(lab, record, image_id):
                raise ValueError('Manager running digest differs from the published artifact')
            verified.add(image_id)
        rows[node] = {'uid': pod['metadata']['uid'], 'image': image, 'image_id': image_id}
    if set(rows) != targets: raise ValueError('Manager requires exactly one healthy Pod per configured client')
    return {'daemonset_uid': ds['metadata']['uid'], 'pods': rows}


def adopt(lab, relay_pid):
    _, record = plan()
    baseline = load('longhorn-v2-upgrade-baseline.py')
    # Capture owns the current empty-business, hardware, credential and IM checks.
    before = baseline.capture(lab)
    ds = lab.get('daemonset', 'longhorn-manager', NS)
    images = [c['image'] for c in ds['spec']['template']['spec']['containers'] if c['name'] == 'longhorn-manager']
    if len(images) != 1: raise ValueError('Manager DaemonSet has ambiguous containers')
    old = images[0].endswith(':'+OLD)
    if not old and not images[0].endswith(':'+NEW):
        raise ValueError('Manager adoption refuses an unknown prior image')
    prior = runtime(lab, record, old=old)
    if old:
        lab.cache_artifacts({'brinnatt/longhorn-manager:'+NEW})
        lab.run_requests(relay_pid, [])
    lab.installation_health()
    current = runtime(lab, record)
    if (current['daemonset_uid'] != prior['daemonset_uid'] or
            (old and {p['uid'] for p in prior['pods'].values()} & {p['uid'] for p in current['pods'].values()})):
        raise ValueError('Manager rollout replaced its DaemonSet or retained old Pod identities')
    baseline.stable_installation(baseline.capture(lab), before)
    # The immediately following owning linked scenarios supply fsync/remount,
    # migration and restore results. Do not duplicate their business fixtures.
    result = {'rollout_performed': old, 'before': prior, 'after': current, 'baseline': before}
    print('LONGHORN_MANAGER_ADOPT_RESULT '+json.dumps(result, sort_keys=True), flush=True)


def main():
    install = load('longhorn-v2-install.py'); _, lab = install.fixture(); lab.require_lease()
    core = load('longhorn-v2-core.py'); core.verify_transport(lab)
    process = None
    try:
        lab.relay_ensure()
        process = subprocess.Popen([str(BASE/'.venv/bin/python'), str(BASE/'tests/helpers/longhorn-v2-install.py'), 'relay-maintain'],
            pass_fds=(7, 8, 9))
        adopt(lab, process.pid)
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=30)
        lab.relay_cleanup()
        lab.core_identity()
    if process.returncode != 0: raise ValueError('Manager adoption relay did not finalize successfully')
    print('LONGHORN_MANAGER_ADOPT_SCOPED_CLEAN_PASS', flush=True)
    print('LONGHORN_MANAGER_ADOPT_PASS', flush=True)


if __name__ == '__main__':
    main()
