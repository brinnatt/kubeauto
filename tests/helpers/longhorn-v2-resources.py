#!/usr/bin/env python3
"""Owned, temporary hugepage fixture for the separate V2 qualification pool.

Uses the official runtime page-pool procedure; never edits GRUB or disks.
An ambiguous remote write retains its journal rather than guessing ownership.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

from importlib.util import module_from_spec, spec_from_file_location

BASE = Path('/usr/local/kubeauto')
PAGES = 1024


def full_material():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('longhorn_full_material', Path(__file__).with_name('longhorn-full-material.py'))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def evidence_path(historical, *, optional=False):
    return full_material().path(BASE, historical, optional=optional)


def host_script():
    return '''import json,re,subprocess,sys
from pathlib import Path
request=json.load(sys.stdin)
pages=Path('/sys/kernel/mm/hugepages/hugepages-2048kB')
def snapshot():
 info={key:int(value) for key,value in re.findall(r'^(MemAvailable|HugePages_Rsvd):\\s+(\\d+)',Path('/proc/meminfo').read_text(),re.M)}
 return {'machine_id':Path('/etc/machine-id').read_text().strip(),
  'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
  'total':int((pages/'nr_hugepages').read_text()),
  'free':int((pages/'free_hugepages').read_text()),
  'reserved':info['HugePages_Rsvd'],'available_kib':info['MemAvailable'],
  'kubelet_active':subprocess.run(['systemctl','is-active','kubelet'],text=True,capture_output=True,timeout=15).stdout.strip()}
before=snapshot()
if request['action']=='resize':
 expected=request['expected'];target=request['target']
 for key in ('machine_id','boot_id','total','free','reserved','kubelet_active'):
  assert before[key]==expected[key], 'V2 page-pool input changed: '+key
 assert before['kubelet_active']=='active' and before['free']==before['total'] and before['reserved']==0, 'V2 page pool is in use'
 assert type(target)==int and target in (0,1024), 'V2 page target outside fixture boundary'
 if target>before['total']:
  assert before['available_kib'] >= (target-before['total'])*2048+1024**2, 'V2 page allocation lacks system headroom'
 (pages/'nr_hugepages').write_text(str(target)+'\\n')
elif request['action']!='probe':
 raise ValueError('unknown V2 resource action')
print(json.dumps(snapshot(),sort_keys=True))
'''


def fixture():
    spec = spec_from_file_location('v2_core_resources', BASE/'tests/helpers/longhorn-v2-core.py')
    core = module_from_spec(spec)
    spec.loader.exec_module(core)
    return core.fixture()[1]


def product():
    spec = spec_from_file_location('v2_resource_admission', BASE/'roles/longhorn/files/longhorn.py')
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_path(lab):
    return lab.DIRECTORY/'longhorn-v2-resources.json'


def no_storage(lab):
    lab.core_identity()
    if lab.get('namespace', 'longhorn-system') or lab.get('crd', 'volumes.longhorn.io'):
        raise ValueError('V2 page-pool changes require no Longhorn installation')


def probe(lab, node, *, target=None, expected=None):
    request = {'action': 'probe'} if target is None else {
        'action': 'resize', 'target': target, 'expected': expected}
    if target is not None:
        print('LONGHORN_V2_RESOURCE_WAIT node=' + node + ' target_pages=' + str(target) +
              ' timeout_seconds=600', flush=True)
    result = lab.remote(node, ['python3', '-c', host_script()], stdin=json.dumps(request),
                        timeout=600 if target is not None else 120)
    return json.loads(result.stdout)


def qualification_current():
    path = evidence_path('tests/evidence/longhorn-v2-resources-qualification-20261020.json')
    evidence = json.loads(path.read_text())
    if evidence['status'] != 'pass' or evidence['rc'] or evidence['failure_markers']:
        raise ValueError('V2 installation requires successful resource qualification')
    for name in ('tests/helpers/longhorn-v2-resources.py', 'tests/helpers/longhorn-v2-resources.sh',
                 'roles/longhorn/files/longhorn.py'):
        if evidence['frozen_inputs'].get(name) != hashlib.sha256((BASE/name).read_bytes()).hexdigest():
            raise ValueError('V2 resource qualification input changed: ' + name)
    print('LONGHORN_V2_RESOURCE_QUALIFICATION_CURRENT', flush=True)


def validate_host(state, node, row):
    if row['machine_id'] != state['raw_disks'][node]['machine_id']:
        raise ValueError('V2 page-pool host identity changed')
    if row['kubelet_active'] != 'active' or row['reserved'] or row['free'] != row['total']:
        raise ValueError('V2 page pool or kubelet is not available')


def read_record(lab):
    path = record_path(lab)
    if path.is_symlink():
        raise ValueError('symlink V2 resource journal')
    row = json.loads(path.read_text())
    uuid.UUID(row['id'])
    state = lab.owner()
    if (row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid'] or
            set(row['hosts']) != set(lab.NODES)):
        raise ValueError('foreign V2 resource journal')
    phases = {'planned', 'write-intent', 'write-confirmed', 'ready',
              'restore-intent', 'restore-confirmed', 'restored'}
    for node, host in row['hosts'].items():
        original, current = host['original'], host['current']
        validate_host(state, node, original)
        if (original['total'] != 0 or host['phase'] not in phases or
                type(current['total']) is not int or not 0 <= current['total'] <= PAGES or
                current['free'] != current['total'] or current['reserved'] != 0 or
                any(current[key] != original[key] for key in ('machine_id', 'boot_id', 'kubelet_active'))):
            raise ValueError('V2 resource journal exceeds its original fixture boundary')
        if ((host['phase'] == 'ready' and current['total'] != PAGES) or
                (host['phase'] in ('planned', 'restored', 'restore-confirmed') and current['total'] != 0)):
            raise ValueError('V2 resource journal phase contradicts its page count')
    return row


def refresh_kubelet(lab, node, pages):
    lab.remote(node, ['systemctl', 'restart', 'kubelet'], timeout=120)
    name = 'longhorn-node-' + node.rsplit('.', 1)[1]
    admission = product()
    def ready():
        current = lab.get('node', name)
        if current['metadata']['uid'] != lab.owner()['node_uids'][name]:
            raise ValueError('V2 kubelet refresh changed node identity')
        return (any(c['type'] == 'Ready' and c['status'] == 'True'
                    for c in current['status']['conditions']) and
                admission.quantity(current['status']['allocatable'].get('hugepages-2Mi', 0)) == pages*2*1024**2)
    lab.wait('v2-kubelet-hugepage-capacity-' + node, 180, ready)


def prepare(lab):
    no_storage(lab)
    lab.verify_core_smoke()
    path = record_path(lab)
    if path.exists() or path.is_symlink():
        raise ValueError('V2 resources require a new owned journal, not adoption')
    state = lab.owner()
    hosts = {}
    # Qualify all hosts before the first write.
    for node in lab.NODES:
        row = probe(lab, node)
        validate_host(state, node, row)
        print('LONGHORN_V2_RESOURCE_INPUT node=' + node + ' total_pages=' + str(row['total']) +
              ' available_kib=' + str(row['available_kib']) +
              ' required_available_kib=' + str(PAGES*2048 + 1024**2), flush=True)
        if row['total'] or row['available_kib'] < PAGES*2048 + 1024**2:
            raise ValueError('V2 fixture requires an empty page pool and sufficient available memory: node=' + node)
        hosts[node] = {'original': row, 'current': row, 'phase': 'planned'}
    record = {'id': str(uuid.uuid4()), 'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'], 'hosts': hosts}
    lab.write_private(path, record)
    for node, row in hosts.items():
        row['phase'] = 'write-intent'
        lab.write_private(path, record)
        row['current'] = probe(lab, node, target=PAGES, expected=row['original'])
        row['phase'] = 'write-confirmed'
        lab.write_private(path, record)
        validate_host(state, node, row['current'])
        if row['current']['total'] != PAGES:
            raise ValueError('kernel could not allocate the complete V2 page pool')
        refresh_kubelet(lab, node, PAGES)
        row['phase'] = 'ready'
        lab.write_private(path, record)
        print('LONGHORN_V2_HUGEPAGES_READY node=' + node + ' pages=1024 capacity_bytes=2147483648', flush=True)


def verify(lab):
    lab.core_identity()
    record = read_record(lab)
    admission = product()
    config = {'longhorn_v2_cpu_mask': '0x1', 'longhorn_v2_cpu_cores': 0,
              'longhorn_v2_cpu_reservation_percentage': 25, 'longhorn_v2_memory_mib': 2048,
              'longhorn_v2_hugepages': True}
    for node, row in record['hosts'].items():
        if row['phase'] != 'ready':
            raise ValueError('V2 page pool is not completely prepared')
        current = probe(lab, node)
        for key in ('machine_id', 'boot_id', 'total'):
            if current[key] != row['current'][key]:
                raise ValueError('V2 prepared resource identity changed')
        admission.check_v2_resources(config, lab.get('node', 'longhorn-node-' + node.rsplit('.', 1)[1]))
    print('LONGHORN_V2_HUGEPAGE_RESOURCE_ADMISSION_PASS nodes=3 mask=0x1 reservation_percent=25', flush=True)


def restore(lab):
    no_storage(lab)
    path = record_path(lab)
    if not path.exists() and not path.is_symlink():
        print('LONGHORN_V2_RESOURCES_SCOPED_CLEAN_PASS state=unchanged')
        return
    record = read_record(lab)
    # Prove every host is safe before restoring the first one.
    for node, row in record['hosts'].items():
        if row['phase'] in ('write-intent', 'restore-intent'):
            raise ValueError('ambiguous V2 page-pool write; preserve journal for diagnosis')
        actual = probe(lab, node)
        validate_host(lab.owner(), node, actual)
        for key in ('machine_id', 'boot_id', 'total', 'free', 'reserved', 'kubelet_active'):
            if actual[key] != row['current'][key]:
                raise ValueError('V2 page pool changed outside its journal')
    for node, row in record['hosts'].items():
        if row['phase'] != 'restored':
            if row['current']['total'] != row['original']['total']:
                row['phase'] = 'restore-intent'
                lab.write_private(path, record)
                row['current'] = probe(lab, node, target=row['original']['total'], expected=row['current'])
                row['phase'] = 'restore-confirmed'
                lab.write_private(path, record)
                if row['current']['total'] != row['original']['total']:
                    raise ValueError('V2 original page pool not restored')
            refresh_kubelet(lab, node, row['original']['total'])
            row['phase'] = 'restored'
            lab.write_private(path, record)
    archive = path.with_name('longhorn-v2-resources-restored-' + record['id'] + '.json')
    if archive.exists() or archive.is_symlink():
        raise ValueError('V2 resource recovery archive already exists')
    path.rename(archive)
    print('LONGHORN_V2_RESOURCES_SCOPED_CLEAN_PASS pages=original kubelet_capacity=original nodes=3', flush=True)


def resolve_completed_write(lab):
    """Resolve only the recorded first allocation failure, never generic adoption."""
    no_storage(lab)
    path = record_path(lab)
    raw = path.read_bytes()
    record = read_record(lab)
    evidence_path = BASE/'tests/evidence/longhorn-v2-resources-allocation-first-failure-20261020.json'
    evidence = json.loads(evidence_path.read_text())
    digest = hashlib.sha256(raw).hexdigest()
    if (evidence['status'] != 'fail' or evidence['rc'] != 1 or
            evidence['resource_journal_sha256'] != digest or evidence['resource_journal_id'] != record['id']):
        raise ValueError('V2 write resolution requires the exact preserved first-failure journal')
    observation = evidence['allocation_observation']
    node = observation['host']
    if node not in lab.NODES or record['hosts'][node]['phase'] != 'write-intent':
        raise ValueError('V2 write resolution is outside the diagnosed host/phase')
    writer = observation['writer']
    command_digest = hashlib.sha256(b'python3\0-c\0' + host_script().encode() + b'\0').hexdigest()
    if writer['command_sha256'] != command_digest or writer['boot_id'] != record['hosts'][node]['original']['boot_id']:
        raise ValueError('V2 diagnosed writer differs from the exact allocation program/boot')
    # Reused PIDs, a surviving allocation or another instance of that program
    # invalidate this narrow proof. Never signal or attach to another process.
    script = '''import hashlib,json,sys
from pathlib import Path
row=json.load(sys.stdin)
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==row['boot_id'], 'changed allocation boot'
assert not (Path('/proc')/str(row['pid'])).exists(), 'allocation PID still exists or was reused'
for path in Path('/proc').iterdir():
 if not path.name.isdigit(): continue
 try: raw=(path/'cmdline').read_bytes()
 except FileNotFoundError: continue
 assert hashlib.sha256(raw).hexdigest()!=row['command_sha256'], 'allocation program still active'
print('writer_absent')
'''
    if lab.remote(node, ['python3', '-c', script], stdin=json.dumps(writer)).stdout.strip() != 'writer_absent':
        raise ValueError('missing V2 allocation quiescence proof')
    actual = {}
    for address, row in record['hosts'].items():
        current = probe(lab, address)
        validate_host(lab.owner(), address, current)
        if current['boot_id'] != row['original']['boot_id']:
            raise ValueError('V2 resolution host rebooted outside its journal')
        expected = PAGES if address == node else 0
        if current['total'] != expected or (address != node and row['phase'] != 'planned'):
            raise ValueError('V2 resolution does not match the one completed allocation')
        actual[address] = current
    archive = path.with_name('longhorn-v2-resources-ambiguous-' + record['id'] + '.json')
    if archive.exists() or archive.is_symlink() or path.read_bytes() != raw:
        raise ValueError('V2 ambiguous resource journal changed during diagnosis')
    lab.write_private(archive, record)
    record['hosts'][node].update(current=actual[node], phase='write-confirmed')
    record['diagnosed_write_resolution'] = {'original_sha256': digest, 'evidence': str(evidence_path.relative_to(BASE))}
    lab.write_private(path, record)
    print('LONGHORN_V2_RESOURCE_WRITE_COMPLETION_PROVEN node=' + node +
          ' pages=1024 free=1024 writer=absent original_journal=archived', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'verify', 'restore', 'resolve-completed-write', 'qualification-current'))
    args = parser.parse_args()
    lab = fixture()
    lab.require_lease()
    if args.mode == 'qualification-current':
        qualification_current()
    else:
        {'prepare': prepare, 'verify': verify, 'restore': restore,
         'resolve-completed-write': resolve_completed_write}[args.mode](lab)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, TimeoutError) as error:
        print('LONGHORN_V2_RESOURCES_FAIL reason=' + str(error), file=sys.stderr)
        sys.exit(1)
