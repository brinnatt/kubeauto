#!/usr/bin/env python3
"""Adopt the published TLS-fixed IM through setup09 on fixed V1/V2 baselines.

This gate preserves cluster, namespace and disk identities. It never uninstalls,
formats a disk or silently rolls back product source after a failed adoption.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import types

import yaml

BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'
RECORD = 'longhorn-v2-im-adopt.json'
OLD_TAG = 'v1.12.1'
NEW_TAG = 'v1.12.1-kubeauto.1'
SOURCE = 'roles/longhorn/files/longhorn.py'
V1_QUALIFICATION = 'tests/evidence/longhorn-idempotence-20261021.json'
V2_QUALIFICATION = 'tests/evidence/longhorn-v2-interrupt-manager-partial-20261021.json'
TARGET_QUALIFICATION = 'tests/evidence/longhorn-v2-dr-target-install-qualification-20261020.json'
ENGINES = ('v1', 'v2', 'v2-dr-target')
OLD_LINE = "    image = c['longhorn_image_prefix'] + '/longhorn-instance-manager:v' + c['longhorn_version']\n"
NEW_LINES = ("    tags = json.loads((FILES / 'images.json').read_text())[c['longhorn_version']]\n"
             "    image = c['longhorn_image_prefix'] + '/longhorn-instance-manager:' + tags['longhorn-instance-manager']\n")


def qualification(engine):
    name = {'v1': V1_QUALIFICATION, 'v2': V2_QUALIFICATION,
            'v2-dr-target': TARGET_QUALIFICATION}[engine]
    accepted = json.loads((BASE/name).read_text())
    if (accepted['status'] != 'pass' or accepted['rc'] or accepted['failure_markers'] or
            accepted['remote_input_verification']['mismatches'] or not accepted.get('independent_readback')):
        raise ValueError('IM adoption requires an independently accepted, healthy scoped baseline')
    if engine == 'v2-dr-target':
        readback = accepted['independent_readback']
        expected = {'192.168.47.149'}
        if (readback.get('baseline_identity') != accepted.get('baseline_identity') or
                set(accepted['baseline_identity'].get('raw_disks', {})) != expected or
                set(accepted['baseline_identity'].get('node_uids', {})) != {'longhorn-node-149'} or
                readback.get('native_block_receipt_sha256') != accepted.get('native_block_receipt_sha256') or
                readback.get('previous_native_uuid_preserved') is not True or
                readback.get('remaining_business') != 0 or readback.get('relays') != 0 or
                accepted.get('business', {}).get('replicas') != 1 or
                accepted['business'].get('cross_node') is not False or
                accepted['business'].get('actual_detached') is not True):
            raise ValueError('target IM adoption requires its independently accepted single-node installation')
        for dependency in ('tests/helpers/longhorn-v2-dr-target.py',
                           'tests/helpers/longhorn-v2-dr-target-install.py'):
            if digest(BASE/dependency) != accepted['frozen_inputs'][dependency]:
                raise ValueError('target IM adoption prerequisite changed: '+dependency)
        return accepted
    if engine == 'v2':
        if accepted.get('signed_scope') != 'v2-interrupt-manager':
            raise ValueError('V2 IM adoption requires its accepted interrupt/manager scope')
        return json.loads((BASE/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').read_text())
    snapshot = 'tests/evidence/longhorn-snapshot-20261020.json'
    readback = accepted['independent_readback']
    if (accepted.get('case') != 'LONGHORN-07' or accepted.get('signed_scope') != 'idempotence' or
            readback.get('installation_healthy') is not True or
            readback.get('credential_uids_match_original_state') is not True or
            readback.get('baseline_identity') != accepted.get('baseline_identity') or
            digest(BASE/snapshot) != accepted['frozen_inputs'][snapshot]):
        raise ValueError('V1 IM adoption requires its accepted idempotence and original identity')
    original = json.loads((BASE/snapshot).read_text())
    if any(accepted['baseline_identity'].get(k) != v for k, v in original['baseline_identity'].items()):
        raise ValueError('V1 IM adoption readback identity differs from the original snapshot')
    return dict(accepted, baseline_identity=original['baseline_identity'])


def prefix(engine):
    return 'LONGHORN_'+engine.upper().replace('-', '_')+'_IM_ADOPT'


def required_nodes(lab, engine):
    """Use the same rendered client/V2 allowlists as product.healthy()."""
    config = json.loads((lab.DIRECTORY/'yml/longhorn/input.json').read_text())
    if engine == 'v1':
        addresses = config.get('longhorn_client_nodes', list(lab.WORKERS))
        expected = lab.WORKERS
    else:
        addresses = config['longhorn_v2_nodes']
        expected = lab.NODES
    if len(addresses) != len(set(addresses)) or set(addresses) != set(expected):
        raise ValueError('IM adoption rendered client inventory differs from its fixed baseline')
    return {'longhorn-node-'+address.rsplit('.', 1)[1] for address in addresses}


def cache(lab):
    lab.cache_artifacts({'brinnatt/longhorn-instance-manager:'+NEW_TAG})


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('adoption input must be a regular non-symlink file: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def previous_source(evidence):
    """Prove the complete product diff, before using the old read-only checker."""
    path = BASE/SOURCE
    digest(path)
    current = path.read_text()
    if current.count(NEW_LINES) != 1 or OLD_LINE in current:
        raise ValueError('adoption requires the exact reviewed IM health pin change')
    original = current.replace(NEW_LINES, OLD_LINE)
    previous_sha = hashlib.sha256(original.encode()).hexdigest()
    if previous_sha != evidence['frozen_inputs'][SOURCE]:
        # Preserve the already accepted single-replica compatibility boundary;
        # the image adoption cannot grant a new exception to unrelated source.
        compatible = evidence.get('compatible_inputs', {}).get(SOURCE, {})
        if (compatible.get('original_sha256') != evidence['frozen_inputs'][SOURCE] or
                compatible.get('sha256') != previous_sha or
                digest(BASE/compatible['evidence']) != compatible['evidence_sha256']):
            raise ValueError('IM adoption refuses unrelated product changes')
    for name in ('roles/longhorn/files/block-check.py', 'tests/helpers/longhorn-lab.py',
                 'tests/helpers/longhorn-v2-lab.py', 'tests/helpers/longhorn-v2-core.py'):
        if digest(BASE/name) != evidence['frozen_inputs'][name]:
            if name == 'tests/helpers/longhorn-lab.py':
                # Bind the sole publication-reference update to every frozen byte.
                current = (BASE/name).read_text()
                old_inventory = 'tests/evidence/longhorn-images-20261019.json'
                new_inventory = 'tests/evidence/longhorn-images-20261021.json'
                if current.count(new_inventory) == 1 and old_inventory not in current:
                    original_inventory = current.replace(new_inventory, old_inventory)
                    if hashlib.sha256(original_inventory.encode()).hexdigest() == evidence['frozen_inputs'][name]:
                        continue
            raise ValueError('IM adoption prerequisite changed: '+name)
    catalog = BASE/'roles/longhorn/files/images.json'
    digest(catalog)
    catalog_text = catalog.read_text()
    tags = json.loads(catalog_text)
    if tags['1.12.1']['longhorn-instance-manager'] != NEW_TAG:
        raise ValueError('IM adoption requires its reviewed published image pin')
    new_entry = '"longhorn-instance-manager": "'+NEW_TAG+'"'
    old_entry = '"longhorn-instance-manager": "'+OLD_TAG+'"'
    if (catalog_text.count(new_entry) != 1 or
            hashlib.sha256(catalog_text.replace(new_entry, old_entry).encode()).hexdigest() !=
            evidence['frozen_inputs']['roles/longhorn/files/images.json']):
        raise ValueError('IM adoption refuses unrelated image catalog changes')
    return original


def empty(lab):
    records = ('longhorn-install-business.json', 'longhorn-encryption-key.json',
               'longhorn-request-input.json', 'longhorn-reclaim-class.json')
    if (any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in records) or
            lab.get('volumes.longhorn.io', namespace=NS)['items'] or
            any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io'
                for p in lab.get('pv')['items']) or
            any(n['metadata']['name'].startswith('longhorn-business-') for n in lab.get('namespaces')['items'])):
        raise ValueError('IM adoption requires an empty owned business baseline')
    for kind in ('engines.longhorn.io', 'enginefrontends.longhorn.io', 'replicas.longhorn.io'):
        if lab.get(kind, namespace=NS)['items']:
            raise ValueError('IM adoption refuses native consumers: '+kind)


def filesystem_disks(lab):
    state = lab.owner()
    if set(state['disks']) != set(lab.WORKERS):
        raise ValueError('V1 IM adoption requires its original three filesystem disks')
    script = '''import hashlib,json,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin);p=Path('/etc/kubeauto/longhorn-disk.json')
assert p.is_file() and not p.is_symlink()
data=p.read_bytes();receipt=json.loads(data)
assert receipt['path']==r['path'] and receipt['uuid']==r['uuid']
mount=json.loads(subprocess.run(['findmnt','--json','--mountpoint',r['path'],'--output','TARGET,FSTYPE,UUID,OPTIONS'],check=True,text=True,capture_output=True,timeout=30).stdout)['filesystems']
assert len(mount)==1 and mount[0]['target']==r['path'] and mount[0]['uuid']==r['uuid']
assert mount[0]['fstype'] in ('ext4','xfs') and 'rw' in mount[0]['options'].split(',')
print(json.dumps({'receipt_sha256':hashlib.sha256(data).hexdigest(),'mount':mount[0]},sort_keys=True))
'''
    receipts = {}
    for node, row in state['disks'].items():
        # Existing fixture check branches are read-only and retain host guards.
        lab.remote(node, ['python3', '-c', lab.disk_parent_script(), 'check'], stdin=json.dumps(row))
        lab.remote(node, ['python3', '-c', lab.disk_cleanup_script(), 'check'], stdin=json.dumps(row))
        receipts[node] = json.loads(lab.remote(node, ['python3', '-c', script], stdin=json.dumps(row)).stdout)
    nodes = lab.get('nodes.longhorn.io', namespace=NS)['items']
    names = {n['metadata']['name'] for n in nodes}
    if (not required_nodes(lab, 'v1') <= names or not names <= set(state['node_uids']) or
            len(names) != len(nodes)):
        raise ValueError('V1 IM adoption native node topology changed')
    native = {}
    for node in nodes:
        name = node['metadata']['name']
        entries = node['spec'].get('disks', {})
        expected = '192.168.122.'+name.rsplit('-', 1)[1] in lab.WORKERS
        if len(entries) != int(expected):
            raise ValueError('V1 IM adoption filesystem disk allowlist changed')
        native[name] = {'uid': node['metadata']['uid'], 'disks': {}}
        for key, disk in entries.items():
            uuid = node['status'].get('diskStatus', {}).get(key, {}).get('diskUUID')
            if disk.get('diskType', 'filesystem') != 'filesystem' or disk['path'] != lab.DATA or not uuid:
                raise ValueError('V1 IM adoption native filesystem disk identity changed')
            native[name]['disks'][key] = {'uuid': uuid, 'spec': disk}
    return {'host_receipts': receipts, 'native_nodes': native}


def baseline(lab, evidence, engine='v2'):
    lab.core_identity()
    state = lab.owner()
    if any(state.get(k) != v for k, v in evidence['baseline_identity'].items()):
        raise ValueError('IM adoption installation identity changed')
    for kind, name, key in (('namespace', NS, 'namespace_uid'),
                            ('secret', 'longhorn-grpc-tls', 'tls_uid'),
                            ('secret', 'longhorn-volume-key', 'encryption_uid')):
        current = lab.get(kind, name, '' if kind == 'namespace' else NS)
        if not current or current['metadata']['uid'] != state[key]:
            raise ValueError('IM adoption namespace or credential identity changed')
    if engine == 'v1':
        empty(lab)
        return {'baseline_identity': evidence['baseline_identity'], 'filesystem_disks': filesystem_disks(lab),
                'required_manager_nodes': sorted(required_nodes(lab, engine)),
                'config_sha256': digest(lab.DIRECTORY/'config.yml'),
                'snapshot_identity': lab.shared_snapshot_identity()}
    receipt = lab.DIRECTORY/'yml/longhorn/block-disks.json'
    if digest(receipt) != evidence['native_block_receipt_sha256']:
        raise ValueError('IM adoption native block receipt changed')
    disks = {}
    for row in json.loads(receipt.read_text())['nodes'].values():
        node = lab.get('nodes.longhorn.io', row['name'], NS)
        if not node or node['metadata']['uid'] != row['longhorn_node_uid']:
            raise ValueError('IM adoption native node identity changed')
        for name, disk in row['disks'].items():
            uuid = node['status'].get('diskStatus', {}).get(name, {}).get('diskUUID')
            if not uuid or uuid != disk['disk_uuid']:
                raise ValueError('IM adoption raw disk UUID changed')
            disks[row['name']+'/'+name] = uuid
    empty(lab)
    return {'baseline_identity': evidence['baseline_identity'],
            'required_manager_nodes': sorted(required_nodes(lab, engine)),
            'native_block_receipt_sha256': digest(receipt), 'disk_uuids': disks,
            'config_sha256': digest(lab.DIRECTORY/'config.yml'),
            'snapshot_identity': lab.shared_snapshot_identity()}


def managers(lab, image, *, allow_transition=False, engine='v2'):
    expected = required_nodes(lab, engine)
    rows = lab.get('instancemanagers.longhorn.io', namespace=NS)['items']
    if (len(rows) != len(expected) or {r['spec'].get('nodeID') for r in rows} != expected or
            any(r['spec'].get('dataEngine') != ('v1' if engine == 'v1' else 'v2') or r['spec'].get('type') != 'aio' or
                r['spec'].get('image') != image or r['metadata'].get('deletionTimestamp') or
                r.get('status', {}).get('currentState') != 'running' for r in rows)):
        if allow_transition:
            return None
        raise ValueError('IM adoption has not converged to its exact required AIO managers')
    setting = lab.get('settings.longhorn.io', 'default-instance-manager-image', NS)
    if not setting or setting.get('value') != image:
        if allow_transition:
            return None
        raise ValueError('IM adoption default image setting did not converge')
    return {r['metadata']['name']: {'uid': r['metadata']['uid'], 'node': r['spec']['nodeID'],
                                  'image': image} for r in rows}


def old_health(lab, original):
    """Use the SHA-proven original checker only before the product upgrade."""
    product = types.ModuleType('longhorn_pre_adoption_health')
    product.__file__ = str(BASE/SOURCE)
    exec(compile(original, product.__file__, 'exec'), product.__dict__)
    c = json.loads((lab.DIRECTORY/'yml/longhorn/input.json').read_text())
    c.setdefault('longhorn_client_nodes', list(lab.WORKERS))
    api = product.API(str(lab.DIRECTORY/'kubectl.kubeconfig'))
    if not product.healthy(c, api, product.node_map(c, api)):
        raise ValueError('IM adoption original installation is unhealthy')


def adopt(install, lab, relay_pid, engine='v2'):
    path = lab.DIRECTORY/RECORD.replace('v2', engine)
    if path.exists() or path.is_symlink():
        raise ValueError('previous IM adoption journal requires explicit recovery')
    evidence = qualification(engine)
    if evidence['status'] != 'pass' or evidence['rc'] or evidence['failure_markers']:
        raise ValueError('IM adoption requires completed AIO installation evidence')
    original = previous_source(evidence)
    before = baseline(lab, evidence, engine)
    old_health(lab, original)
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    if config['longhorn_version'] != '1.12.1' or config.get('longhorn_data_engines', ['v1']) != ['v1' if engine == 'v1' else 'v2']:
        raise ValueError('IM adoption requires the original single-engine 1.12.1 profile')
    image_prefix = config['longhorn_image_prefix']+'/longhorn-instance-manager:'
    old = managers(lab, image_prefix+OLD_TAG, engine=engine)
    journal = {'run_id': lab.owner()['run_id'], 'engine': engine, 'phase': 'setup-intent', 'before': before,
               'old_managers': old, 'product_sha256': digest(BASE/SOURCE),
               'image_catalog_sha256': digest(BASE/'roles/longhorn/files/images.json')}
    lab.write_private(path, journal)
    print(prefix(engine)+'_PRECONDITIONS_VERIFIED '+json.dumps(before, sort_keys=True), flush=True)
    # The existing reversible request fixture invokes config.yml + kubecli setup09.
    lab.run_requests(relay_pid, [])
    install.health(lab)
    # Product readiness can precede the controller's deletion of empty old IMs.
    new = lab.wait('im-adopt-old-managers-retired', 600,
                   lambda: managers(lab, image_prefix+NEW_TAG, allow_transition=True, engine=engine))
    if set(r['uid'] for r in old.values()) & set(r['uid'] for r in new.values()):
        raise ValueError('IM adoption reused an old manager identity')
    if baseline(lab, evidence, engine) != before:
        raise ValueError('IM adoption changed its baseline identities or configuration')
    journal.update(phase='business-intent', new_managers=new)
    lab.write_private(path, journal)
    print(prefix(engine)+'_RUNTIME_VERIFIED '+json.dumps(new, sort_keys=True), flush=True)
    if engine == 'v2-dr-target':
        install.business(lab, relay_pid)
    else:
        lab.business('install', relay_pid=relay_pid)
    install.health(lab)
    if baseline(lab, evidence, engine) != before or managers(lab, image_prefix+NEW_TAG, engine=engine) != new:
        raise ValueError('IM adoption business changed its baseline or manager identities')
    journal['phase'] = 'verified'
    lab.write_private(path, journal)
    print(prefix(engine)+'_BUSINESS_PASS', flush=True)


def finish(install, lab, engine='v2'):
    path = lab.DIRECTORY/RECORD.replace('v2', engine)
    if path.is_symlink() or not path.is_file():
        raise ValueError('IM adoption finalization requires its original journal')
    row = json.loads(path.read_text())
    if row['run_id'] != lab.owner()['run_id'] or row['phase'] != 'verified' or row['engine'] != engine:
        raise ValueError('IM adoption refuses to finalize an incomplete or foreign attempt')
    evidence = qualification(engine)
    previous_source(evidence)
    install.health(lab)
    if (baseline(lab, evidence, engine) != row['before'] or
            digest(BASE/SOURCE) != row['product_sha256'] or
            digest(BASE/'roles/longhorn/files/images.json') != row['image_catalog_sha256']):
        raise ValueError('IM adoption finalization inputs or identities changed')
    image = next(iter(row['new_managers'].values()))['image']
    if managers(lab, image, engine=engine) != row['new_managers']:
        raise ValueError('IM adoption finalization manager identity changed')
    print(prefix(engine)+'_RESULT '+json.dumps(row, sort_keys=True), flush=True)
    path.unlink()
    print(prefix(engine)+'_FIXTURES_CLEAN_PASS', flush=True)


def clean(lab, relay_pid, engine='v2'):
    """Recover only this attempt's request/business fixtures; keep its journal."""
    path = lab.DIRECTORY/RECORD.replace('v2', engine)
    if not path.exists() and not path.is_symlink():
        return
    digest(path)
    row = json.loads(path.read_text())
    state = lab.owner()
    if (row['run_id'] != state['run_id'] or row['engine'] != engine or
            row['before']['baseline_identity'].get('namespace_uid') != state['namespace_uid']):
        raise ValueError('IM adoption recovery refuses a foreign journal')
    lab.core_identity()
    namespace = lab.get('namespace', NS)
    if not namespace or namespace['metadata']['uid'] != state['namespace_uid']:
        raise ValueError('IM adoption recovery namespace changed')
    request = lab.DIRECTORY/'longhorn-request-input.json'
    if request.exists() or request.is_symlink():
        digest(request)
        receipt = json.loads(request.read_text())
        if (receipt['run_id'] != state['run_id'] or
                hashlib.sha256(receipt['original'].encode()).hexdigest() != row['before']['config_sha256']):
            raise ValueError('IM adoption recovery refuses a foreign request')
        lab.restore_request_input()
    if digest(lab.DIRECTORY/'config.yml') != row['before']['config_sha256']:
        raise ValueError('IM adoption recovery original configuration changed')
    if row['phase'] in ('business-intent', 'verified'):
        lab.cleanup_business(relay_pid=relay_pid)
    elif row['phase'] != 'setup-intent':
        raise ValueError('IM adoption recovery phase is unknown')
    empty(lab)
    print(prefix(engine)+'_RECOVERY_FIXTURES_CLEAN_PASS journal=retained image=retained', flush=True)


def verify_log(log, engine='v2'):
    lines = log.splitlines()
    structured = []
    for marker in (prefix(engine)+'_PRECONDITIONS_VERIFIED ',
                   prefix(engine)+'_RUNTIME_VERIFIED ', prefix(engine)+'_RESULT '):
        rows = [json.loads(line[len(marker):]) for line in lines if line.startswith(marker)]
        if len(rows) != 1:
            raise ValueError('IM adoption requires exactly one structured marker: '+marker)
        structured.append(rows[0])
    before, runtime, result = structured
    expected_nodes = {'longhorn-node-'+str(n) for n in
                      {'v1': (212, 165, 238), 'v2': (146, 147, 148), 'v2-dr-target': (149,)}[engine]}
    if (result.get('phase') != 'verified' or result.get('engine') != engine or result.get('before') != before or
            set(before.get('required_manager_nodes', [])) != expected_nodes or
            result.get('new_managers') != runtime or len(runtime) != len(expected_nodes) or
            len(result.get('old_managers', {})) != len(runtime) or
            {r.get('node') for r in runtime.values()} != expected_nodes or
            {r.get('node') for r in result['old_managers'].values()} != expected_nodes or
            set(r['uid'] for r in runtime.values()) &
            set(r['uid'] for r in result['old_managers'].values()) or
            any(not r['image'].endswith(':'+NEW_TAG) for r in runtime.values())):
        raise ValueError('IM adoption structured evidence does not bind old/new identities')
    business_marker = ('LONGHORN_V2_DR_TARGET_INSTALL_BUSINESS_PASS' if engine == 'v2-dr-target'
                       else 'LONGHORN_INSTALL_BUSINESS_PASS')
    for marker in (prefix(engine)+'_BUSINESS_PASS', business_marker,
                   'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', prefix(engine)+'_FIXTURES_CLEAN_PASS'):
        if lines.count(marker) != 1:
            raise ValueError('IM adoption missing or duplicate marker: '+marker)
    if engine == 'v2-dr-target':
        remounts = re.findall(r'^LONGHORN_V2_DR_TARGET_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=([a-f0-9]{64}) replicas=1 cross_node=false$', log, re.M)
        if len(remounts) != 1 or 'LONGHORN_FSYNC_REMOUNT_VERIFIED ' in log:
            raise ValueError('target IM adoption requires its single-replica same-node fsync/remount')
    else:
        remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
        if len(remounts) != 1 or set(remounts[0][1].split(',')) != expected_nodes:
            raise ValueError('IM adoption requires fsync/remount across the qualified three replicas')
    if re.search(r'^LONGHORN_(?:FIRST_FAILURE|CLEANUP_FAIL|PRODUCT_FAIL|PREFLIGHT_FAIL)\b', log, re.M):
        raise ValueError('IM adoption log contains a failure marker')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'finish', 'clean', 'cache', 'verify'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    parser.add_argument('--engine', choices=ENGINES, default='v2')
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log:
            parser.error('--log is required')
        verify_log(args.log.read_text(), args.engine)
        return
    name = {'v1': 'longhorn-lab.py', 'v2': 'longhorn-v2-install.py',
            'v2-dr-target': 'longhorn-v2-dr-target-install.py'}[args.engine]
    spec = importlib.util.spec_from_file_location('im_adopt_fixture', BASE/'tests/helpers'/name)
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)
    if args.engine == 'v1':
        lab = install
        sys.modules[lab.__name__] = lab
        install = types.SimpleNamespace(health=lambda lab: lab.installation_health())
    else:
        _, lab = install.fixture()
    lab.require_lease()
    if args.mode == 'business':
        adopt(install, lab, args.relay_pid, args.engine)
    elif args.mode == 'clean':
        clean(lab, args.relay_pid, args.engine)
    elif args.mode == 'cache':
        cache(lab)
    else:
        finish(install, lab, args.engine)


if __name__ == '__main__':
    main()
