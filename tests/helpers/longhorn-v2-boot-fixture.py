#!/usr/bin/env python3
"""Runner-owned one-reboot hugepage/multipath fixture for LONGHORN-50.

Longhorn 1.12.1 hugepage-configuration requires preallocation. systemd
sysctl.d(5), systemd-sysctl.service(8) and systemd.unit(5) explain early boot
ordering; a oneshot before sysinit/kubelet records actual zero -> 1024 pages.
No GRUB, sysctl defaults, disk bytes or product settings are changed.
"""
import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import uuid

RECORD = 'longhorn-v2-boot-fixture.json'
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def allocate(request, root='/'):
    """ExecStart on the single admitted new boot; independent standard library."""
    import json
    import os
    from pathlib import Path
    import re
    import subprocess
    import tempfile

    root = Path(root); directory = root/request['directory'].lstrip('/')
    receipt = directory/'boot.json'
    if receipt.exists() or receipt.is_symlink(): raise ValueError('previous boot allocation intent exists')
    machine = (root/'etc/machine-id').read_text().strip()
    boot = (root/'proc/sys/kernel/random/boot_id').read_text().strip()
    if machine != request['machine_id'] or boot == request['old_boot_id']:
        raise ValueError('boot allocation requires its original machine and a new boot')
    def snapshot():
        values = {k: int(v) for k, v in re.findall(r'^(MemAvailable|HugePages_Rsvd):\s+(\d+)',
                                                           (root/'proc/meminfo').read_text(), re.M)}
        pages = root/'sys/kernel/mm/hugepages/hugepages-2048kB'
        return {'machine_id': machine, 'boot_id': boot, 'total': int((pages/'nr_hugepages').read_text()),
                'free': int((pages/'free_hugepages').read_text()), 'reserved': values['HugePages_Rsvd'],
                'available_kib': values['MemAvailable']}
    def persist(row):
        with tempfile.NamedTemporaryFile('w', dir=directory, delete=False) as stream:
            os.fchmod(stream.fileno(), 0o600); json.dump(row, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno()); temporary = Path(stream.name)
        temporary.replace(receipt)
        fd = os.open(directory, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    before = snapshot()
    if before['total'] or before['free'] or before['reserved'] or before['available_kib'] < 3*1024**2:
        raise ValueError('boot allocation requires zero idle pages and system memory headroom')
    kubelet = subprocess.run(['systemctl', 'is-active', 'kubelet'], capture_output=True, text=True, timeout=20)
    if kubelet.stdout.strip() not in ('inactive', 'activating'):
        raise ValueError('boot allocation must precede kubelet startup')
    row = {'id': request['id'], 'run_id': request['run_id'], 'cluster_uid': request['cluster_uid'],
           'volume_uid': request['volume_uid'], 'original': before, 'phase': 'allocation-intent'}
    persist(row)
    (root/'sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages').write_text('1024\n')
    after = snapshot(); row['current'] = after; row['phase'] = 'allocation-confirmed'; persist(row)
    if after['total'] != 1024 or after['free'] != 1024 or after['reserved']:
        raise ValueError('boot allocation did not produce 1024 completely available pages')
    row['phase'] = 'ready'; persist(row)


def unit(request):
    return ('[Unit]\nDescription=Kubeauto leased Longhorn V2 reboot page fixture\n'
            'DefaultDependencies=no\nAfter=local-fs.target systemd-modules-load.service systemd-sysctl.service\n'
            'Before=sysinit.target kubelet.service docker.service containerd.service cri-docker.service\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            'ExecStart=/usr/bin/python3 '+request['directory']+'/allocate.py\nTimeoutStartSec=180\n')


def host_fixture(request, action, root='/'):
    """Create/remove only exact new artifacts; persistent masks last one reboot.

    Each public symlink is hard-linked from its journaled private inode, so
    losing a response cannot turn an unrelated mask into an owned artifact.
    """
    import hashlib
    import json
    import os
    from pathlib import Path
    import stat
    import subprocess
    import tempfile
    import uuid

    root = Path(root); uuid.UUID(request['id']); uuid.UUID(request['run_id'])
    if action not in ('arm', 'inspect', 'remove'): raise ValueError('invalid boot fixture action')
    def path(name): return root/name.lstrip('/')
    directory = path(request['directory']); journal = directory/'fixture.json'
    if request['directory'] != '/var/lib/kubeauto-longhorn-v2-boot-'+request['id']:
        raise ValueError('boot fixture directory identity differs')
    machine = path('/etc/machine-id').read_text().strip()
    boot = path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if machine != request['machine_id']: raise ValueError('boot fixture machine changed')
    unit_name = 'kubeauto-longhorn-v2-pages-'+request['id']+'.service'
    public_unit = '/etc/systemd/system/'+unit_name
    enable = '/etc/systemd/system/sysinit.target.wants/'+unit_name
    dependency = '/etc/systemd/system/kubelet.service.d/50-kubeauto-longhorn-'+request['id']+'.conf'
    def run(argv):
        return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=30).stdout
    def units():
        return {name: dict(line.split('=', 1) for line in run(['systemctl', 'show', name,
                    '--property=LoadState,ActiveState,UnitFileState']).splitlines() if '=' in line)
                for name in ('multipathd.service', 'multipathd.socket')}
    def identity(filename):
        p = path(filename); info = p.lstat()
        value = {'dev': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode),
                 'uid': info.st_uid, 'gid': info.st_gid}
        if stat.S_ISLNK(info.st_mode): value['target'] = os.readlink(p)
        elif stat.S_ISREG(info.st_mode): value['sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
        else: raise ValueError('boot artifact is not a regular file or symlink')
        return value
    def persist(row):
        with tempfile.NamedTemporaryFile('w', dir=directory, delete=False) as stream:
            os.fchmod(stream.fileno(), 0o600); json.dump(row, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno()); temporary = Path(stream.name)
        temporary.replace(journal)
        fd = os.open(directory, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    if action == 'arm':
        if boot != request['old_boot_id'] or units() != request['multipath']['current']['units']:
            raise ValueError('boot fixture original service or boot changed')
        for name, state in units().items():
            if state['ActiveState'] != 'inactive': raise ValueError('boot fixture requires leased stopped services')
        if directory.exists() or directory.is_symlink(): raise ValueError('previous remote boot fixture exists')
        masks = ['/etc/systemd/system/'+name for name, state in units().items() if state['LoadState'] == 'loaded']
        for name in (public_unit, enable, dependency, *masks):
            if path(name).exists() or path(name).is_symlink(): raise ValueError('foreign boot fixture target exists')
        directory.mkdir(mode=0o700)
        row = {'request': request, 'phase': 'arm-intent', 'files': {}, 'private_links': [], 'directories': {}}; persist(row)
        def parent(filename):
            parent = path(filename).parent
            if not parent.exists():
                parent.mkdir(mode=0o755)
                info = parent.stat(); row['directories'][str(parent.relative_to(root))] = {'dev': info.st_dev, 'inode': info.st_ino}
                persist(row)
            if parent.is_symlink() or not parent.is_dir(): raise ValueError('foreign boot artifact parent')
        for name, content in ((request['directory']+'/allocate.py', request['script']), (public_unit, request['unit']),
                (dependency, '[Unit]\nRequires='+unit_name+'\nAfter='+unit_name+'\n')):
            parent(name)
            with os.fdopen(os.open(path(name), os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600), 'w') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            row['files'][name] = identity(name); persist(row)
        for index, (name, target) in enumerate([(enable, public_unit), *((name, '/dev/null') for name in masks)]):
            private = request['directory']+'/link-'+str(index)
            os.symlink(target, path(private)); row['private_links'].append(private)
            row['files'][private] = identity(private); persist(row)
            parent(name)
            # The persisted private inode is also the only admitted public one.
            row['files'][name] = row['files'][private]; persist(row)
            os.link(path(private), path(name), follow_symlinks=False)
            if identity(name) != row['files'][name]: raise ValueError('boot fixture symlink publication changed inode')
        run(['systemctl', 'daemon-reload'])
        for name in masks:
            state = units()[Path(name).name]
            if state['LoadState'] != 'masked' or state['ActiveState'] != 'inactive':
                raise ValueError('owned persistent multipath mask did not converge')
        row['phase'] = 'armed'; persist(row)
    else:
        if directory.is_symlink() or journal.is_symlink(): raise ValueError('symlink remote boot fixture')
        row = json.loads(journal.read_text())
        if row['request'] != request or row['phase'] != 'armed':
            raise ValueError('remote boot fixture identity or phase changed')
    for filename, expected in row['files'].items():
        if identity(filename) != expected: raise ValueError('boot artifact identity changed')
    receipt = directory/'boot.json'; allocation = None
    if receipt.exists() or receipt.is_symlink():
        if receipt.is_symlink() or not receipt.is_file(): raise ValueError('foreign boot allocation receipt')
        allocation = json.loads(receipt.read_text())
        if any(allocation[k] != request[k] for k in ('id', 'run_id', 'cluster_uid', 'volume_uid')):
            raise ValueError('boot allocation receipt ownership differs')
    result = {'id': request['id'], 'machine_id': machine, 'boot_id': boot,
              'manifest': row, 'allocation': allocation}
    if action == 'remove':
        allowed = {'fixture.json', 'allocate.py', *(Path(n).name for n in row['private_links'])}
        if allocation is not None: allowed.add('boot.json')
        if {p.name for p in directory.iterdir()} != allowed:
            raise ValueError('foreign file in boot fixture directory')
        # Check all identities before the first unlink, including inode-bound
        # masks. Never invoke systemctl unmask against an unowned pathname.
        for filename, expected in row['files'].items():
            if identity(filename) != expected: raise ValueError('boot artifact changed before retirement')
        def kubelet():
            return dict(line.split('=', 1) for line in run(['systemctl', 'show', 'kubelet.service',
                '--property=ActiveState,Requires']).splitlines() if '=' in line)
        before_kubelet = kubelet()
        if before_kubelet.get('ActiveState') not in ('active', 'inactive'):
            raise ValueError('boot fixture retirement requires a stable kubelet state')
        # Requires propagates an explicit stop to kubelet. Retire only our
        # inode-bound edge and reload it before stopping the page allocator.
        path(dependency).unlink()
        run(['systemctl', 'daemon-reload'])
        if unit_name in kubelet().get('Requires', '').split():
            raise ValueError('boot fixture dependency remains loaded; do not stop allocator')
        for filename, expected in row['files'].items():
            if filename != dependency and identity(filename) != expected:
                raise ValueError('boot artifact changed before allocator stop')
        run(['systemctl', 'stop', unit_name])
        if path(dependency).exists() or path(dependency).is_symlink():
            raise ValueError('foreign boot dependency appeared during retirement')
        for filename, expected in row['files'].items():
            if filename != dependency and identity(filename) != expected:
                raise ValueError('boot artifact changed after allocator stop')
        for filename in row['files']:
            if filename != dependency: path(filename).unlink()
        for name, expected in row['directories'].items():
            p = root/name; info = p.stat()
            if p.is_symlink() or {'dev': info.st_dev, 'inode': info.st_ino} != expected:
                raise ValueError('boot artifact parent identity changed')
            # Keep unrelated files created by their real owner during the run.
            if not list(p.iterdir()): p.rmdir()
        if allocation is not None: receipt.unlink()
        journal.unlink(); directory.rmdir(); run(['systemctl', 'daemon-reload'])
        if units() != request['multipath']['current']['units']:
            raise ValueError('boot fixture retirement changed original stopped service state')
        after_kubelet = kubelet()
        if before_kubelet.get('ActiveState') != after_kubelet.get('ActiveState'):
            raise ValueError('boot fixture retirement changed kubelet active state')
        result['kubelet_retirement'] = {'before': before_kubelet, 'after': after_kubelet}
        result['removed'] = True
    return result


def remote(lab, request, action):
    code = inspect.getsource(host_fixture)+'\nimport json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(host_fixture(r["request"],r["action"]),sort_keys=True))\n'
    return json.loads(lab.remote(request['node'], ['python3', '-c', code],
                                stdin=json.dumps({'request': request, 'action': action}), timeout=240).stdout)


def restore_completed_kubelet(request, completed, root='/'):
    """Repair only the old completed fixture's propagated clean kubelet stop."""
    import hashlib
    from pathlib import Path
    import re
    import subprocess
    import uuid

    root = Path(root); uuid.UUID(request['id'])
    removed = completed.get('removed', {}); current = completed.get('current', {})
    manifest = removed.get('manifest', {})
    unit_name = 'kubeauto-longhorn-v2-pages-'+request['id']+'.service'
    dependency = '/etc/systemd/system/kubelet.service.d/50-kubeauto-longhorn-'+request['id']+'.conf'
    dependency_sha = hashlib.sha256(('[Unit]\nRequires='+unit_name+'\nAfter='+unit_name+'\n').encode()).hexdigest()
    if (completed.get('phase') != 'complete' or completed.get('request') != request or
            completed.get('run_id') != request['run_id'] or not removed.get('removed') or
            'kubelet_retirement' in removed or manifest.get('request') != request or
            manifest.get('files', {}).get(dependency, {}).get('sha256') != dependency_sha or
            current.get('kubelet_active') != 'active' or
            removed.get('boot_id') != current.get('boot_id') or
            current.get('boot_id') == request['old_boot_id'] or
            request['directory'] != '/var/lib/kubeauto-longhorn-v2-boot-'+request['id']):
        raise ValueError('kubelet recovery requires the exact legacy completed fixture')
    machine = (root/'etc/machine-id').read_text().strip()
    boot = (root/'proc/sys/kernel/random/boot_id').read_text().strip()
    if machine != request['machine_id'] or machine != current['machine_id'] or boot != current['boot_id']:
        raise ValueError('completed fixture kubelet recovery changed machine or boot')
    for filename in (*manifest['files'], request['directory']):
        path = root/filename.lstrip('/')
        if path.exists() or path.is_symlink():
            raise ValueError('completed fixture kubelet recovery found a remaining or foreign artifact')
    def show(name, properties):
        result = subprocess.run(['systemctl', 'show', name, '--property='+properties],
            check=True, text=True, capture_output=True, timeout=30)
        return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    before = show('kubelet.service', 'LoadState,ActiveState,SubState,Result,Requires')
    if before.get('LoadState') != 'loaded' or unit_name in before.get('Requires', '').split():
        raise ValueError('completed fixture kubelet dependency has not retired')
    pages = root/'sys/kernel/mm/hugepages/hugepages-2048kB'
    if int((pages/'nr_hugepages').read_text()) != 1024:
        raise ValueError('completed fixture page allocation changed; do not start kubelet')
    if before.get('ActiveState') == 'active':
        return {'started': False, 'machine_id': machine, 'boot_id': boot, 'before': before, 'after': before}
    reserved = re.search(r'^HugePages_Rsvd:\s+(\d+)', (root/'proc/meminfo').read_text(), re.M)
    if (before.get('ActiveState') != 'inactive' or before.get('SubState') != 'dead' or
            before.get('Result') != 'success' or not reserved or int(reserved[1]) or
            int((pages/'free_hugepages').read_text()) != 1024):
        raise ValueError('completed fixture recovery requires a clean stopped kubelet and unused pages')
    for name, expected in request['multipath']['current']['units'].items():
        if show(name, 'LoadState,ActiveState,UnitFileState') != expected:
            raise ValueError('completed fixture multipath lease changed')
    subprocess.run(['systemctl', 'start', 'kubelet.service'], check=True,
        text=True, capture_output=True, timeout=120)
    after = show('kubelet.service', 'LoadState,ActiveState,SubState,Result,Requires')
    if after.get('ActiveState') != 'active' or unit_name in after.get('Requires', '').split():
        raise ValueError('completed fixture kubelet service did not recover')
    return {'started': True, 'machine_id': machine, 'boot_id': boot, 'before': before, 'after': after}


def recover_kubelet(lab, journal, proof):
    """Existing failure-cleanup path only; a service repair is no business PASS."""
    path = lab.DIRECTORY/RECORD
    if not path.exists() and not path.is_symlink(): return
    state = bound(lab, journal, proof)
    if path.is_symlink(): raise ValueError('symlink boot fixture journal')
    row = json.loads(path.read_text()); request = row['request']
    if row['phase'] != 'complete': return
    if (row['run_id'] != state['run_id'] or request['node'] != proof['address'] or
            request['volume_uid'] != journal['volume_uid'] or request['run_id'] != state['run_id'] or
            request['cluster_uid'] != state['cluster_uid'] or
            request['machine_id'] != proof['original_host']['machine_id']):
        raise ValueError('completed fixture kubelet recovery refuses a foreign journal')
    # New retirement already proved kubelet remained active. Do not use this
    # historical repair to conceal unrelated later service failures.
    if 'kubelet_retirement' in row.get('removed', {}): return
    row['kubelet_recovery_intent'] = {'boot_id': row['current']['boot_id']}
    lab.write_private(path, row)
    code = inspect.getsource(restore_completed_kubelet)+'\nimport json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(restore_completed_kubelet(r["request"],r["completed"]),sort_keys=True))\n'
    row['kubelet_recovery'] = json.loads(lab.remote(request['node'], ['python3', '-c', code],
        stdin=json.dumps({'request': request, 'completed': row}), timeout=180).stdout)
    lab.write_private(path, row)
    print('LONGHORN_V2_REBOOT_FIXTURE_KUBELET_RECOVERED node='+request['node']+' id='+request['id'], flush=True)


def check_inactive_kubelet(lab, journal, proof):
    """An already retired fixture cannot legitimately wait for boot startup."""
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink boot fixture journal')
    if path.exists():
        bound(lab, journal, proof)
        if json.loads(path.read_text())['phase'] == 'complete':
            raise ValueError('kubelet inactive after completed boot fixture; use scoped failure cleanup')


def bound(lab, journal, proof):
    lab.require_lease(); state = lab.owner()
    load('longhorn-v2-runtime-faults.py').validate_intent(lab, journal, proof)
    if tuple(lab.NODES) != NODES or proof['case'] != 'worker-reboot' or proof['address'] not in NODES:
        raise ValueError('boot fixture requires its exact source-pool worker reboot')
    if journal.get('v2_runtime_fault') != proof:
        raise ValueError('boot fixture requires the durable runtime reboot intent')
    return state


def arm(lab, journal, proof):
    state = bound(lab, journal, proof); node = proof['address']; path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink(): raise ValueError('previous boot fixture needs scoped recovery')
    resources = load('longhorn-v2-resources.py'); pages = resources.read_record(lab)
    host = resources.probe(lab, node); entry = state['host_state']['nodes'][node]['multipath']
    if (pages['hosts'][node]['phase'] != 'ready' or entry['phase'] != 'stopped' or
            host['machine_id'] != proof['original_host']['machine_id'] or host['boot_id'] != proof['original_host']['boot_id'] or
            host['total'] != 1024 or pages['hosts'][node]['current']['boot_id'] != host['boot_id'] or
            pages['hosts'][node]['original']['total'] != 0 or entry['current']['boot_id'] != host['boot_id']):
        raise ValueError('boot fixture original resource or service lease changed')
    common = load('longhorn-v2-install.py'); common.multipath(lab, node, 'check', expected=entry['current'])
    load('longhorn-v2-post-reboot-environment.py').consumers(lab, node, host['boot_id'])
    identifier = str(uuid.uuid4())
    request = {'id': identifier, 'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'],
               'volume_uid': journal['volume_uid'], 'node': node, 'machine_id': host['machine_id'],
               'old_boot_id': host['boot_id'], 'directory': '/var/lib/kubeauto-longhorn-v2-boot-'+identifier,
               'multipath': copy.deepcopy(entry)}
    request['script'] = inspect.getsource(allocate)+'\nallocate('+repr({k: v for k, v in request.items() if k != 'multipath'})+')\n'
    request['unit'] = unit(request)
    row = {'run_id': state['run_id'], 'request': request, 'phase': 'arm-intent',
           'resource_sha256': hashlib.sha256(resources.record_path(lab).read_bytes()).hexdigest(),
           'state_sha256': hashlib.sha256(lab.STATE.read_bytes()).hexdigest()}
    lab.write_private(path, row)
    row['armed'] = remote(lab, request, 'arm'); row['phase'] = 'armed'; lab.write_private(path, row)
    print('LONGHORN_V2_REBOOT_FIXTURE_ARMED node='+node+' id='+identifier, flush=True)


def complete(lab, journal, proof):
    """Observe/retire before native recovery polling; no runtime page resize."""
    state = bound(lab, journal, proof); path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink boot fixture journal')
    row = json.loads(path.read_text()); request = row['request']; node = proof['address']
    if (row['run_id'] != state['run_id'] or request['node'] != node or request['volume_uid'] != journal['volume_uid']):
        raise ValueError('foreign boot fixture journal')
    if row['phase'] == 'complete': return True
    if row['phase'] != 'armed': raise ValueError('ambiguous boot fixture transition requires diagnosis')
    observed = remote(lab, request, 'inspect')
    if observed['boot_id'] == request['old_boot_id']: return False
    allocation = observed['allocation']
    if (not allocation or allocation['phase'] != 'ready' or allocation['original']['total'] or
            allocation['current']['boot_id'] != observed['boot_id'] or
            allocation['current']['machine_id'] != request['machine_id'] or
            allocation['current']['total'] != 1024 or allocation['current']['free'] != 1024 or
            allocation['current']['reserved']):
        raise ValueError('reboot lacks its measured pre-kubelet 1024-page allocation')
    resources = load('longhorn-v2-resources.py'); resource_path = resources.record_path(lab)
    resource_raw = resource_path.read_bytes(); state_raw = lab.STATE.read_bytes()
    if (hashlib.sha256(resource_raw).hexdigest() != row['resource_sha256'] or
            hashlib.sha256(state_raw).hexdigest() != row['state_sha256']):
        raise ValueError('boot fixture host or resource journal changed outside its lease')
    current = resources.probe(lab, node)
    if (current['machine_id'] != request['machine_id'] or current['boot_id'] != observed['boot_id'] or
            current['total'] != 1024 or current['kubelet_active'] != 'active'):
        raise ValueError('post-reboot actual page pool or kubelet does not match its boot receipt')
    # Independent current kubelet admission, not a restart of a running node.
    resources.product().check_v2_resources({'longhorn_v2_cpu_mask': '0x1', 'longhorn_v2_cpu_cores': 0,
        'longhorn_v2_cpu_reservation_percentage': 25, 'longhorn_v2_memory_mib': 2048, 'longhorn_v2_hugepages': True},
        lab.get('node', proof['node']))
    archive = load('longhorn-v2-resources-reboot.py').archive_bytes
    for name, raw in (('resources', resource_raw), ('host-state', state_raw)):
        archive(lab.DIRECTORY/('longhorn-v2-boot-'+request['id']+'-'+name+'.json'), raw)
    row.update(phase='ledger-update-intent', observed=observed, current=current); lab.write_private(path, row)
    pages = resources.read_record(lab)
    # Allocation/free counters are measured before kubelet; its active state
    # is measured now. Keep both complete observations as explicit provenance.
    pages['hosts'][node] = {'phase': 'ready', 'original': dict(allocation['original'], kubelet_active=current['kubelet_active']),
                           'current': dict(allocation['current'], kubelet_active=current['kubelet_active']),
                           'reboot_fixture': {'id': request['id'], 'boot_allocation': allocation, 'runtime_observation': current}}
    state['host_state']['nodes'][node]['multipath']['original']['boot_id'] = current['boot_id']
    state['host_state']['nodes'][node]['multipath']['current']['boot_id'] = current['boot_id']
    lab.write_private(resource_path, pages); lab.write_private(lab.STATE, state)
    row['phase'] = 'remove-intent'; lab.write_private(path, row)
    row['removed'] = remote(lab, request, 'remove')
    common = load('longhorn-v2-install.py')
    common.multipath(lab, node, 'check', expected=state['host_state']['nodes'][node]['multipath']['current'])
    row['phase'] = 'complete'; lab.write_private(path, row)
    print('LONGHORN_V2_REBOOT_FIXTURE_RESTORED node='+node+' id='+request['id'], flush=True)
    return True


def clean(lab, journal, proof):
    """Normal/failure exit: cancel an unexecuted reboot, or verify its receipt."""
    path = lab.DIRECTORY/RECORD
    if not path.exists() and not path.is_symlink(): return
    state = bound(lab, journal, proof); row = json.loads(path.read_text())
    if row['run_id'] != state['run_id'] or row['request']['volume_uid'] != journal['volume_uid']:
        raise ValueError('boot fixture cleanup refuses a foreign journal')
    if row['phase'] == 'armed':
        current = remote(lab, row['request'], 'inspect')
        if current['boot_id'] == row['request']['old_boot_id']:
            row['phase'] = 'cancel-intent'; lab.write_private(path, row)
            row['removed'] = remote(lab, row['request'], 'remove')
            row['phase'] = 'cancelled'; lab.write_private(path, row)
        elif not complete(lab, journal, proof):
            raise ValueError('boot fixture reboot has not converged')
        row = json.loads(path.read_text())
    if row['phase'] not in ('complete', 'cancelled'):
        raise ValueError('ambiguous boot fixture cleanup retains its journal')
    path.rename(path.with_name('longhorn-v2-boot-fixture-completed-'+row['request']['id']+'.json'))
    print('LONGHORN_V2_REBOOT_FIXTURE_SCOPED_CLEAN_PASS', flush=True)
