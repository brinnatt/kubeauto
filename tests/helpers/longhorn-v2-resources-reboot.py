#!/usr/bin/env python3
"""Recover the leased V2 pool's volatile hugepages after an observed reboot.

This is environment recovery, not LONGHORN-44 acceptance. It preserves the
pre-reboot journal byte for byte and never writes disks or product settings.
An interrupted or ambiguous allocation retains its intent for diagnosis.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

BASE = Path('/usr/local/kubeauto')
RECORD = 'longhorn-v2-resources-reboot.json'
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
TARGET_NODES = ('192.168.47.149',)
NS = 'longhorn-system'
MODES = {'v2-resources-reboot-recovery': 'source',
         'v2-dr-target-resources-reboot-recovery': 'target'}


def marker(profile):
    if profile not in ('source', 'target'):
        raise ValueError('invalid reboot recovery profile')
    return 'LONGHORN_V2_'+('DR_TARGET_' if profile == 'target' else '')+'RESOURCES_REBOOT_RECOVERY'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def disk_identity(lab):
    """Read the stable device identity, including the original machine guard."""
    script = '''import json,stat,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin)
assert Path('/etc/machine-id').read_text().strip()==r['machine_id'],'changed disk host'
p=Path(r['path']);real=p.resolve(strict=True)
assert stat.S_ISBLK(real.stat().st_mode),'not a block device'
rows=json.loads(subprocess.run(['lsblk','--json','--bytes','--nodeps','--output','PATH,TYPE,SIZE,WWN,SERIAL',str(real)],check=True,text=True,capture_output=True,timeout=30).stdout)['blockdevices']
assert len(rows)==1,'ambiguous raw disk'
d=rows[0]
assert d['path']==str(real) and d['type']=='disk','changed device path/type'
assert r['identity'] and r['identity'] in (d.get('wwn'),d.get('serial')),'changed hardware identity'
assert int(d['size'])==r['size_bytes'],'changed raw disk size'
assert p.resolve(strict=True)==real,'changed stable device link'
print(json.dumps({'identity':r['identity'],'size_bytes':int(d['size']),'path':r['path']},sort_keys=True))
'''
    return {node: json.loads(lab.remote(node, ['python3', '-c', script],
                stdin=json.dumps(lab.owner()['raw_disks'][node])).stdout) for node in lab.NODES}


def idle(lab, adopt):
    adopt.empty(lab)
    for manager in lab.get('instancemanagers.longhorn.io', namespace=NS)['items']:
        status = manager.get('status', {})
        if (manager['spec'].get('dataEngine') != 'v2' or
                any(status.get(key) for key in ('instances', 'instanceEngines', 'instanceReplicas'))):
            raise ValueError('reboot recovery refuses native manager consumers')


def archive_bytes(path, raw):
    """O_EXCL prevents replacing another operation's private provenance."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def completed_transition(lab, progress, record):
    """Validate a completed receipt before admitting a later reboot.

    A ready resource ledger may include subsequent runner-owned boot fixtures.
    Its normal reader validates those current observations; this receipt binds
    the earlier recovery and its immutable input, not the latest host boot.
    """
    if progress.is_symlink():
        raise ValueError('previous reboot recovery intent requires diagnosis before retry')
    if not progress.exists():
        return None
    raw = progress.read_bytes()
    old = json.loads(raw)
    receipt = record.get('reboot_recovery', {})
    if (old.get('phase') != 'complete' or old.get('run_id') != record['run_id'] or
            old.get('cluster_uid') != record['cluster_uid'] or old.get('resource_id') != record['id'] or
            set(old.get('hosts', {})) != set(lab.NODES) or receipt != {
                'archive': old.get('archive'), 'original_sha256': old.get('original_sha256'),
                'transition': RECORD}):
        raise ValueError('previous reboot recovery intent requires diagnosis before retry')
    sha = old['original_sha256']
    expected = 'longhorn-v2-resources-pre-reboot-'+record['id']+'-'+sha[:12]+'.json'
    archive = lab.DIRECTORY/expected
    if (old['archive'] != expected or archive.is_symlink() or
            hashlib.sha256(archive.read_bytes()).hexdigest() != sha):
        raise ValueError('previous reboot recovery archive changed')
    for node, host in old['hosts'].items():
        if (host['phase'] != 'ready' or host['current']['total'] != 1024 or
                host['current']['machine_id'] != record['hosts'][node]['current']['machine_id']):
            raise ValueError('previous reboot recovery host identity changed')
    return raw


def recover(lab, resources, adopt, installation, *, profile='source', evidence=None, verify=None):
    prefix = marker(profile)
    nodes = NODES if profile == 'source' else TARGET_NODES
    if profile == 'target' and not callable(verify):
        raise ValueError('target reboot recovery requires its single-node resource verifier')
    lab.require_lease()
    lab.core_identity()
    if tuple(lab.NODES) != nodes or set(lab.owner()['raw_disks']) != set(nodes):
        raise ValueError('reboot recovery requires the exact original '+profile+' V2 pool')
    path = resources.record_path(lab)
    if path.is_symlink() or not path.is_file():
        raise ValueError('reboot recovery requires its regular resource journal')
    progress = lab.DIRECTORY/RECORD
    raw = path.read_bytes()
    record = resources.read_record(lab)
    completed = completed_transition(lab, progress, record)
    if profile == 'source':
        if evidence is not None:
            raise ValueError('source reboot recovery requires its fixed qualification')
        material = load('longhorn-full-material.py')
        if material.active():
            evidence = json.loads(material.path(BASE,
                'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').read_text())
            if (evidence.get('status') != 'pass' or evidence.get('rc') != 0 or
                    evidence.get('failure_markers') != 0 or
                    evidence.get('remote_input_verification', {}).get('mismatches') != 0 or
                    not evidence.get('independent_readback')):
                raise ValueError('reboot recovery requires its accepted current full installation')
        else:
            evidence = adopt.qualification('v2')
    elif (not evidence or evidence.get('status') != 'pass' or evidence.get('rc') or
            evidence.get('failure_markers') or evidence.get('remote_input_verification', {}).get('mismatches') != 0 or
            evidence.get('independent_readback', {}).get('baseline_identity') != evidence.get('baseline_identity')):
        raise ValueError('target reboot recovery requires its fixed independently accepted installation')
    # This is recovery of the installed version, not a new IM adoption. Use
    # the established exact-pin/accepted-adoption dependency checker, including
    # its reviewed compatibility chain, before any host write.
    for name in ('roles/longhorn/files/longhorn.py', 'roles/longhorn/files/block-check.py',
                 'tests/helpers/longhorn-lab.py', 'tests/helpers/longhorn-v2-lab.py',
                 'tests/helpers/longhorn-v2-core.py'):
        if not installation.qualification_input_current(evidence, name):
            raise ValueError('reboot recovery installation prerequisite changed: '+name)
    before = adopt.baseline(lab, evidence, 'v2')
    idle(lab, adopt)
    disks = disk_identity(lab)
    observed = {}
    # Admit every host before publishing any intent or mutating a page pool.
    for node, host in record['hosts'].items():
        old = host['current']
        if host['phase'] != 'ready' or old['total'] != resources.PAGES:
            raise ValueError('reboot recovery requires the previously ready 1024-page pool')
        current = resources.probe(lab, node)
        if current['boot_id'] == old['boot_id']:
            if (any(current[key] != old[key] for key in ('machine_id', 'total', 'kubelet_active')) or
                    current['reserved'] or not 0 <= current['free'] <= current['total']):
                raise ValueError('unrebooted resource host changed outside its journal')
        else:
            resources.validate_host(lab.owner(), node, current)
            if current['total'] or current['available_kib'] < resources.PAGES*2048 + 1024**2:
                raise ValueError('reboot recovery requires a changed boot, zero pages and memory headroom')
        observed[node] = current
    if all(host['boot_id'] == record['hosts'][node]['current']['boot_id']
           for node, host in observed.items()):
        raise ValueError('reboot recovery requires at least one changed boot')
    if path.read_bytes() != raw:
        raise ValueError('resource journal changed during reboot diagnosis')
    sha = hashlib.sha256(raw).hexdigest()
    archive = path.with_name('longhorn-v2-resources-pre-reboot-'+record['id']+'-'+sha[:12]+'.json')
    archive_bytes(archive, raw)
    transition = {'run_id': record['run_id'], 'cluster_uid': record['cluster_uid'],
                  'resource_id': record['id'], 'original_sha256': sha, 'archive': archive.name,
                  'baseline': before, 'disks': disks, 'hosts': {
                      node: {'previous_boot_id': record['hosts'][node]['current']['boot_id'],
                             'original': current, 'phase': 'planned'} for node, current in observed.items()}}
    if completed is not None:
        if progress.is_symlink() or progress.read_bytes() != completed:
            raise ValueError('completed reboot recovery changed during admission')
        previous_sha = hashlib.sha256(completed).hexdigest()
        previous_archive = 'longhorn-v2-resources-reboot-complete-'+previous_sha+'.json'
        archive_bytes(lab.DIRECTORY/previous_archive, completed)
        transition['previous_transition'] = {'archive': previous_archive, 'sha256': previous_sha}
    lab.write_private(progress, transition)
    for node, host in transition['hosts'].items():
        if host['original']['boot_id'] == host['previous_boot_id']:
            host.update(current=host['original'], phase='ready')
            lab.write_private(progress, transition)
            continue
        idle(lab, adopt)
        host['phase'] = 'write-intent'
        lab.write_private(progress, transition)
        current = resources.probe(lab, node, target=resources.PAGES, expected=host['original'])
        host.update(current=current, phase='write-confirmed')
        lab.write_private(progress, transition)
        resources.validate_host(lab.owner(), node, current)
        if (current['total'] != resources.PAGES or any(current[key] != host['original'][key]
                for key in ('machine_id', 'boot_id', 'kubelet_active'))):
            raise ValueError('reboot recovery allocation did not converge on the admitted boot')
        host['phase'] = 'kubelet-refresh-intent'
        lab.write_private(progress, transition)
        resources.refresh_kubelet(lab, node, resources.PAGES)
        host['phase'] = 'ready'
        lab.write_private(progress, transition)
        print(prefix+'_HUGEPAGES_READY node='+node+' pages=1024', flush=True)
    if path.read_bytes() != raw or hashlib.sha256(archive.read_bytes()).hexdigest() != sha:
        raise ValueError('resource recovery provenance changed')
    for node, host in transition['hosts'].items():
        if host['original']['boot_id'] != host['previous_boot_id']:
            record['hosts'][node] = {'original': host['original'], 'current': host['current'], 'phase': 'ready'}
    record['reboot_recovery'] = {'archive': archive.name, 'original_sha256': sha, 'transition': RECORD}
    transition['phase'] = 'resource-journal-update-intent'
    lab.write_private(progress, transition)
    lab.write_private(path, record)
    (verify or resources.verify)(lab)
    def healthy():
        try:
            lab.installation_health()
        except ValueError as error:
            if str(error) == 'Longhorn control or disk health failed':
                return False
            raise
        return True
    lab.wait('v2-original-installation-after-reboot-resources', 900, healthy)
    idle(lab, adopt)
    if adopt.baseline(lab, evidence, 'v2') != before or disk_identity(lab) != disks:
        raise ValueError('reboot recovery changed original cluster/config/disk identity')
    transition['phase'] = 'complete'
    lab.write_private(progress, transition)
    print(prefix+'_RESULT '+json.dumps({
        'original_sha256': sha, 'archive': archive.name,
        'boots': {node: {'previous': h['previous_boot_id'], 'current': h['current']['boot_id']}
                  for node, h in transition['hosts'].items()}}, sort_keys=True), flush=True)
    print(prefix+'_SCOPED_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=tuple(MODES))
    args = parser.parse_args()
    profile = MODES[args.mode]
    install = load('longhorn-v2-'+('dr-target-' if profile == 'target' else '')+'install.py')
    _, lab = install.fixture()
    options = {'profile': profile}
    if profile == 'target':
        path = BASE/'tests/evidence/longhorn-v2-dr-target-install-qualification-20261020.json'
        if path.is_symlink():
            raise ValueError('symlink target recovery qualification')
        options.update(evidence=json.loads(path.read_text()), verify=install.verify_resources)
    recover(lab, load('longhorn-v2-resources.py'), load('longhorn-v2-im-adopt.py'),
            load('longhorn-v2-install.py'), **options)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, TimeoutError) as error:
        prefix = marker(MODES.get(sys.argv[1] if len(sys.argv)>1 else '', 'source'))
        print(prefix+'_FAIL reason='+str(error), file=sys.stderr)
        sys.exit(1)
