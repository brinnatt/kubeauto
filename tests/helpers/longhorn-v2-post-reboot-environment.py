#!/usr/bin/env python3
"""Recover only originally leased multipath services after a proven reboot.

Environment recovery, not a Longhorn acceptance result. An existing IM adoption
intent retains its original strict recovery path. With no adoption intent, the
source pool must prove its current accepted, healthy empty installation; only
the original stopped multipath lease is renewed for the verified new boot.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

BASE = Path('/usr/local/kubeauto')
RECORD = 'longhorn-v2-post-reboot-environment.json'
INSTALLED_RECORD = 'longhorn-v2-installed-post-reboot-environment.json'
MODES = {'v2-post-reboot-environment-recovery': 'v2',
         'v2-dr-target-post-reboot-environment-recovery': 'v2-dr-target'}
POOLS = {'v2': ('192.168.47.146', '192.168.47.147', '192.168.47.148'),
         'v2-dr-target': ('192.168.47.149',)}


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def consumers_script():
    return '''import json,shutil,stat,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin)
assert Path('/etc/machine-id').read_text().strip()==r['machine_id'],'changed raw disk host'
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==r['boot_id'],'changed reboot admission'
p=Path(r['path']);real=p.resolve(strict=True)
assert stat.S_ISBLK(real.stat().st_mode),'raw device changed type'
rows=json.loads(subprocess.run(['lsblk','--json','--bytes','--nodeps','--output','PATH,TYPE,SIZE,WWN,SERIAL',str(real)],check=True,text=True,capture_output=True,timeout=30).stdout)['blockdevices']
assert len(rows)==1,'ambiguous raw device'
d=rows[0]
assert d['path']==str(real) and d['type']=='disk' and int(d['size'])==r['size_bytes'],'changed raw device'
assert r['identity'] and r['identity'] in (d.get('wwn'),d.get('serial')),'changed raw device identity'
holders=list((Path('/sys/class/block')/real.name/'holders').iterdir())
assert not holders,'raw disk holders prevent multipath recovery'
assert not list(Path('/sys/class/iscsi_session').glob('*')),'iSCSI sessions prevent multipath recovery'
assert not list(Path('/sys/class/iscsi_connection').glob('*')),'iSCSI connections prevent multipath recovery'
if shutil.which('iscsiadm'):
 session=subprocess.run(['iscsiadm','-m','session'],text=True,capture_output=True,timeout=30)
 assert session.returncode==21,'iSCSI session probe is not empty'
dm={p.parent.name:(p/'uuid').read_text().strip() for p in Path('/sys/block').glob('dm-*/dm')}
assert not any(v.startswith('mpath-') for v in dm.values()),'multipath maps prevent recovery'
assert p.resolve(strict=True)==real,'raw device link changed'
print(json.dumps({'machine_id':r['machine_id'],'boot_id':r['boot_id'],'identity':r['identity'],
 'size_bytes':r['size_bytes'],'holders':[],'iscsi_sessions':0,'multipath_maps':0},sort_keys=True))
'''


def consumers(lab, node, boot):
    request = dict(lab.owner()['raw_disks'][node], boot_id=boot)
    return json.loads(lab.remote(node, ['python3', '-c', consumers_script()], stdin=json.dumps(request)).stdout)


def admission(entry, current, machine_id, resource_host, *, allow_unchanged=False):
    if entry['phase'] != 'stopped':
        raise ValueError('multipath recovery requires the previously stopped lease')
    original, previous = entry['original'], entry['current']
    if (original['machine_id'] != machine_id or previous['machine_id'] != machine_id or
            current['machine_id'] != machine_id or original['boot_id'] != previous['boot_id'] or
            (current['boot_id'] == previous['boot_id'] and not allow_unchanged)):
        raise ValueError('multipath recovery requires the same machine and a changed boot')
    if current['boot_id'] == previous['boot_id'] and current != previous:
        raise ValueError('unrebooted multipath host changed outside its journal')
    for key in ('dm', 'configs'):
        if original[key] != previous[key] or current[key] != previous[key]:
            raise ValueError('multipath recovery refuses changed mapping or configuration')
    if set(current['units']) != {'multipathd.service', 'multipathd.socket'} or current['units'].keys() != previous['units'].keys():
        raise ValueError('multipath recovery unit set changed')
    for name, unit in current['units'].items():
        old, initially = previous['units'][name], original['units'][name]
        if old['ActiveState'] != 'inactive':
            raise ValueError('multipath recovery previous lease was not stopped')
        if any(unit.get(k, '') != old.get(k, '') or initially.get(k, '') != old.get(k, '')
               for k in ('LoadState', 'UnitFileState')):
            raise ValueError('multipath recovery refuses changed unit enable or load state')
        if unit['ActiveState'] not in ('active', 'inactive') or (
                unit['ActiveState'] == 'active' and initially['ActiveState'] != 'active'):
            raise ValueError('multipath recovery refuses a newly active foreign service')
    if (resource_host['phase'] != 'ready' or resource_host['current']['machine_id'] != machine_id or
            resource_host['current']['boot_id'] != current['boot_id'] or resource_host['current']['total'] != 1024):
        raise ValueError('multipath recovery requires the accepted current-boot page recovery')


def unchanged_adoption(lab, adopt, engine):
    path = lab.DIRECTORY/adopt.RECORD.replace('v2', engine)
    adopt.digest(path); raw = path.read_bytes(); row = json.loads(raw)
    if (row['run_id'] != lab.owner()['run_id'] or row['engine'] != engine or row['phase'] != 'setup-intent' or
            'new_managers' in row or adopt.digest(BASE/adopt.SOURCE) != row['product_sha256'] or
            adopt.digest(BASE/'roles/longhorn/files/images.json') != row['image_catalog_sha256']):
        raise ValueError('only unchanged setup-intent adoption can be retired')
    evidence = adopt.qualification(engine); previous_source = adopt.previous_source(evidence)
    if adopt.baseline(lab, evidence, engine) != row['before']:
        raise ValueError('failed adoption baseline or original input changed')
    images = {m['image'] for m in row['old_managers'].values()}
    if len(images) != 1 or not next(iter(images)).endswith(':'+adopt.OLD_TAG):
        raise ValueError('failed adoption does not bind the original old image')
    if adopt.managers(lab, next(iter(images)), engine=engine) != row['old_managers']:
        raise ValueError('failed adoption already changed manager identity or image')
    adopt.old_health(lab, previous_source)
    adopt.empty(lab)
    request = lab.DIRECTORY/'longhorn-request-input.json'
    if request.exists() or request.is_symlink():
        raise ValueError('failed adoption request input needs its original scoped cleanup')
    if path.read_bytes() != raw:
        raise ValueError('adoption journal changed during read-only recovery admission')
    return path, raw, row


def installed_baseline(lab, install, resources):
    """Bind a healthy, empty current installation without an adoption intent."""
    if tuple(lab.NODES) != POOLS['v2']:
        raise ValueError('installed environment recovery requires the original source pool')
    for name in ('longhorn-v2-im-adopt.json', 'longhorn-v2-runtime-resources.json'):
        if (lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink():
            raise ValueError('installed environment recovery retains another operation intent')
    install.volume_baseline(lab)
    state = lab.owner()
    managers = lab.get('instancemanagers.longhorn.io', namespace='longhorn-system')['items']
    identities = {}
    for manager in managers:
        name = manager['metadata']['name']
        pod = lab.get('pod', name, 'longhorn-system')
        identities[name] = {'uid': manager['metadata']['uid'], 'spec': manager['spec'],
                            'pod_uid': pod['metadata']['uid']}
    files = (lab.DIRECTORY/'config.yml', lab.DIRECTORY/'yml/longhorn/block-disks.json',
             resources.record_path(lab))
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError('installed environment recovery requires regular original inputs')
    return {'identity': {key: state[key] for key in ('run_id', 'cluster_uid', 'namespace_uid', 'raw_disks')},
            'managers': identities,
            'files': {str(path.relative_to(lab.DIRECTORY)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in files}}


def recover(lab, install, resources, adopt, archive, *, engine='v2', installed=False):
    lab.require_lease(); lab.core_identity()
    if tuple(lab.NODES) != POOLS[engine]:
        raise ValueError('post-reboot recovery refuses an unleased pool')
    state = lab.owner(); state_path = lab.STATE
    if state_path.is_symlink(): raise ValueError('symlink host-state journal')
    state_raw = state_path.read_bytes()
    host_state = state['host_state']['nodes']
    if set(host_state) != set(lab.NODES) or set(state['raw_disks']) != set(lab.NODES):
        raise ValueError('post-reboot recovery requires all original disk and host leases')
    progress = lab.DIRECTORY/(INSTALLED_RECORD if installed else RECORD)
    if progress.exists() or progress.is_symlink():
        raise ValueError('previous environment recovery intent needs diagnosis')
    if installed:
        installation = installed_baseline(lab, install, resources)
    else:
        adoption_path, adoption_raw, adoption = unchanged_adoption(lab, adopt, engine)
    pages = resources.read_record(lab)
    hosts = {}
    # The last host's refusal must prevent writes on every earlier host.
    for node in lab.NODES:
        current = install.multipath(lab, node, 'probe')
        admission(host_state[node]['multipath'], current, state['raw_disks'][node]['machine_id'],
                  pages['hosts'][node], allow_unchanged=installed)
        observed_pages = resources.probe(lab, node)
        if any(observed_pages[k] != pages['hosts'][node]['current'][k] for k in ('machine_id', 'boot_id', 'total')):
            raise ValueError('current page pool changed after accepted recovery')
        hosts[node] = {'before': current, 'consumers': consumers(lab, node, current['boot_id']), 'phase': 'planned'}
    if installed and all(host['before']['boot_id'] == host_state[node]['multipath']['current']['boot_id']
                         for node, host in hosts.items()):
        raise ValueError('installed environment recovery requires at least one changed boot')
    if state_path.read_bytes() != state_raw:
        raise ValueError('host-state journal changed during reboot admission')
    state_sha = hashlib.sha256(state_raw).hexdigest()
    state_archive = lab.DIRECTORY/('longhorn-v2-host-state-pre-reboot-'+state_sha[:16]+'.json')
    archive(state_archive, state_raw)
    transition = {'run_id': state['run_id'], 'engine': engine, 'phase': 'planned',
                  'state_sha256': state_sha, 'state_archive': state_archive.name, 'hosts': hosts}
    if installed:
        transition['installation_before'] = installation
    else:
        transition['adoption_sha256'] = hashlib.sha256(adoption_raw).hexdigest()
    lab.write_private(progress, transition)
    for node, host in hosts.items():
        if host['before']['boot_id'] == host_state[node]['multipath']['current']['boot_id']:
            host.update(after=host['before'], phase='stopped')
            lab.write_private(progress, transition)
            continue
        if consumers(lab, node, host['before']['boot_id']) != host['consumers']:
            raise ValueError('multipath consumer boundary changed before stop')
        host['phase'] = 'stop-intent'; lab.write_private(progress, transition)
        host['after'] = install.multipath(lab, node, 'stop', expected=host['before'])
        expected = copy.deepcopy(host['before'])
        for unit in expected['units'].values(): unit['ActiveState'] = 'inactive'
        if host['after'] != expected:
            raise ValueError('multipath recovery changed unrelated host state')
        host['phase'] = 'stopped'; lab.write_private(progress, transition)
    if state_path.read_bytes() != state_raw or state_archive.read_bytes() != state_raw:
        raise ValueError('host-state recovery provenance changed')
    for node, host in hosts.items():
        entry = host_state[node]['multipath']
        if host['before']['boot_id'] != entry['current']['boot_id']:
            entry['original']['boot_id'] = host['after']['boot_id']
            entry['current'] = host['after']
            entry['post_reboot_recovery'] = {'archive': state_archive.name, 'sha256': state_sha}
    transition['phase'] = 'state-update-intent'; lab.write_private(progress, transition)
    lab.write_private(state_path, state)
    # Revalidate all product inputs and real old manager identities after the
    # environment mutation. Setup-intent is not proof that setup did nothing.
    if installed:
        if installed_baseline(lab, install, resources) != installation:
            raise ValueError('current installation changed while restoring the environment')
    else:
        path, raw, again = unchanged_adoption(lab, adopt, engine)
        if path != adoption_path or raw != adoption_raw or again != adoption:
            raise ValueError('adoption changed while restoring the environment')
    for node, host in hosts.items():
        install.multipath(lab, node, 'check', expected=host['after'])
        if consumers(lab, node, host['after']['boot_id']) != host['consumers']:
            raise ValueError('multipath consumer boundary changed after stop')
    if not installed:
        adoption_sha = hashlib.sha256(raw).hexdigest()
        adoption_archive = path.with_name(path.stem+'-rejected-setup-'+adoption_sha[:16]+'.json')
        archive(adoption_archive, raw)
        transition.update(phase='adoption-retire-intent', adoption_archive=adoption_archive.name)
        lab.write_private(progress, transition)
        if path.read_bytes() != raw or adoption_archive.read_bytes() != raw:
            raise ValueError('failed adoption provenance changed before retirement')
        path.unlink()
    transition['phase'] = 'complete'; lab.write_private(progress, transition)
    marker = 'LONGHORN_'+engine.upper().replace('-', '_')+'_POST_REBOOT_ENVIRONMENT_RECOVERY'
    print(marker+'_RESULT '+json.dumps({'state_sha256': state_sha, 'state_archive': state_archive.name,
        'operation': 'installed-host-recovery' if installed else 'rejected-adoption-recovery',
        'adoption_sha256': None if installed else adoption_sha,
        'adoption_archive': None if installed else adoption_archive.name,
        'boots': {node: {'previous': json.loads(state_raw)['host_state']['nodes'][node]['multipath']['current']['boot_id'],
                         'current': host['after']['boot_id']} for node, host in hosts.items()}}, sort_keys=True), flush=True)
    print(marker+'_SCOPED_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('mode', choices=tuple(MODES))
    engine = MODES[parser.parse_args().mode]
    fixture = load('longhorn-v2-'+('dr-target-' if engine == 'v2-dr-target' else '')+'install.py')
    _, lab = fixture.fixture()
    adopt = load('longhorn-v2-im-adopt.py')
    path = lab.DIRECTORY/adopt.RECORD.replace('v2', engine)
    installed = engine == 'v2' and not path.exists() and not path.is_symlink()
    recover(lab, load('longhorn-v2-install.py'), load('longhorn-v2-resources.py'),
            adopt, load('longhorn-v2-resources-reboot.py').archive_bytes, engine=engine, installed=installed)


if __name__ == '__main__':
    try: main()
    except (ValueError, KeyError, OSError, TimeoutError) as error:
        print('LONGHORN_V2_POST_REBOOT_ENVIRONMENT_RECOVERY_FAIL reason='+str(error), file=sys.stderr)
        sys.exit(1)
