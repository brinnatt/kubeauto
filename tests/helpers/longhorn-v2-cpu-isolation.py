#!/usr/bin/env python3
"""LONGHORN-53 host CPU isolation, before the independent long I/O window.

Manager 8320238242e8c804325d9588ec1db10feb9f36b8 resolves per-IM overrides.
IM cfd63a673e3047ca176185619565d5a81d33ec1c package/start-spdk-tgt programs
IRQ/workqueue/RPS; its clear means all-online, not the original custom policy.
This fixture journals and restores each original mask after native clearing.
"""
import argparse
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import secrets
import sys
import uuid

import yaml

NS = 'longhorn-system'
RECORD = 'longhorn-v2-cpu-isolation.json'
SETTING = 'data-engine-cpu-isolation-enabled'
FIELD = 'longhorn_v2_cpu_isolation_enabled'
FLAGS = ('--enable-irq-affinity', '--enable-workqueue-affinity', '--enable-rps')


def effective(global_enabled, override):
    if type(global_enabled) is not bool or override not in ('', 'true', 'false'):
        raise ValueError('invalid CPU isolation declaration')
    return override == 'true' if override else global_enabled


def host_masks(request, action, *, root='/'):
    """Snapshot before mutation, observe owned policies, restore exact prior masks."""
    import json
    import os
    from pathlib import Path
    import re
    import stat
    import subprocess

    root = Path(root); record = root/('var/lib/kubeauto-longhorn-cpu-'+request['id']+'.json')
    if not re.fullmatch('[a-f0-9-]{36}', request['id']): raise ValueError('invalid isolation fixture id')
    def run(args): return subprocess.run(args, capture_output=True, text=True, check=True, timeout=30)
    def mask(value):
        if not re.fullmatch('[a-fA-F0-9]+(?:,[a-fA-F0-9]{1,8})*', value.strip()):
            raise ValueError('invalid kernel CPU mask')
        return int(value.replace(',', '').strip(), 16)
    def machine():
        return {'machine_id': (root/'etc/machine-id').read_text().strip(),
            'boot_id': (root/'proc/sys/kernel/random/boot_id').read_text().strip(),
            'online': (root/'sys/devices/system/cpu/online').read_text().strip()}
    def online(value):
        bits = 0
        for component in value.split(','):
            ends = [int(v) for v in component.split('-')]
            if len(ends) > 2 or ends[0] > ends[-1]: raise ValueError('invalid online CPU range')
            for cpu in range(ends[0], ends[-1]+1): bits |= 1 << cpu
        if not bits: raise ValueError('empty online CPU range')
        return bits
    def service():
        response = run(['systemctl', 'show', 'irqbalance.service', '-p', 'LoadState', '-p', 'ActiveState', '-p', 'UnitFileState'])
        return dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
    def snapshot():
        paths = [root/'proc/irq/default_smp_affinity', root/'sys/devices/virtual/workqueue/cpumask']
        paths += sorted((root/'proc/irq').glob('[0-9]*/smp_affinity'))
        paths += sorted((root/'sys/devices/virtual/workqueue').glob('*/cpumask'))
        for interface in sorted((root/'sys/class/net').iterdir()):
            if not interface.is_symlink() or 'virtual' in str(interface.readlink()): continue
            paths += sorted(interface.glob('queues/rx-*/rps_cpus'))
        rows = {}
        for path in paths:
            actual = path.resolve()
            if root.resolve() not in actual.parents: raise ValueError('mask path escapes host root')
            info = actual.stat(); value = actual.read_text()
            if not stat.S_ISREG(info.st_mode): raise ValueError('non-file mask interface')
            key = '/'+str(path.relative_to(root)); identity = {'device': info.st_dev, 'inode': info.st_ino,
                'resolved': '/'+str(actual.relative_to(root))}
            if re.fullmatch('/proc/irq/[0-9]+/smp_affinity', key):
                identity['irq'] = {name: (actual.parent/name).read_text().strip() for name in
                    ('actions', 'chip_name', 'hwirq') if (actual.parent/name).exists()}
            rows[key] = {'identity': identity, 'raw': value, 'mask': mask(value)}
        if not any('/rps_cpus' in path for path in rows): raise ValueError('CPU isolation needs physical RX queue RPS interfaces')
        return {'host': machine(), 'masks': rows, 'irqbalance': service()}
    def persist(row):
        temporary = record.with_suffix('.new')
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(row, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(record)
    marker = root/'var/lib/longhorn/instance-manager/v2/spdk_cpu_mask'
    if action == 'capture':
        if record.exists() or record.is_symlink() or marker.exists() or marker.is_symlink():
            raise ValueError('previous host isolation state requires scoped recovery')
        original = snapshot()
        if original['host']['machine_id'] != request['machine_id'] or original['host']['boot_id'] != request['boot_id']:
            raise ValueError('CPU isolation host differs from its raw-disk lease')
        row = {'request': request, 'original': original, 'expected': original, 'phase': 'stop-irqbalance-intent',
            'marker_parents': {str(path.relative_to(root)): path.exists() for path in (marker.parent, marker.parent.parent)}}
        persist(row)
        if original['irqbalance'].get('ActiveState') == 'active': run(['systemctl', 'stop', 'irqbalance.service'])
        row['expected'] = snapshot()
        if (row['expected']['host'] != original['host'] or row['expected']['masks'] != original['masks'] or
                row['expected']['irqbalance'].get('ActiveState') not in ('inactive', 'failed')):
            raise ValueError('host masks changed while acquiring irqbalance lease')
        row['phase'] = 'captured'; persist(row)
    if record.is_symlink(): raise ValueError('symlink CPU isolation journal')
    row = json.loads(record.read_text())
    if row['request'] != request or row['phase'] not in ('captured', 'observed', 'restore-intent', 'restored'):
        raise ValueError('foreign or uncertain CPU isolation journal')
    current = snapshot(); original = row['original']; before = row['expected']
    if current['host'] != original['host'] or set(current['masks']) != set(original['masks']):
        raise ValueError('CPU/IRQ/workqueue/RX queue topology changed during isolation fixture')
    if any(entry['identity'] != original['masks'][path]['identity'] for path, entry in current['masks'].items()):
        raise ValueError('CPU mask interface identity changed')
    if current['irqbalance'] != before['irqbalance']:
        raise ValueError('foreign irqbalance transition during CPU isolation fixture')
    if action in ('enabled', 'cleared'):
        all_online = online(current['host']['online']); spdk = int(request['spdk_mask'], 16)
        expected = all_online & ~spdk if action == 'enabled' else all_online
        if not expected or spdk & ~all_online: raise ValueError('CPU isolation has no housekeeping CPU or selects offline CPUs')
        required = ('/proc/irq/default_smp_affinity', '/sys/devices/virtual/workqueue/cpumask')
        if any(current['masks'][path]['mask'] != expected for path in required):
            raise ValueError('native IRQ/workqueue global isolation does not match SPDK complement')
        rps = [entry for path, entry in current['masks'].items() if path.endswith('/rps_cpus')]
        if not rps or any(entry['mask'] != expected for entry in rps):
            raise ValueError('native physical RX queue RPS isolation did not converge')
        for path, entry in current['masks'].items():
            # Upstream explicitly tolerates kernel-managed per-IRQ/workqueue
            # write rejection. Such unchanged interfaces remain recorded.
            if entry['mask'] not in (expected, before['masks'][path]['mask']):
                raise ValueError('mask changed outside the native isolation transition')
        if action == 'enabled':
            if marker.is_symlink() or not marker.exists() or int(marker.read_text().strip(), 16) != spdk:
                raise ValueError('enabled native isolation lacks its exact persisted SPDK mask')
        elif marker.exists() or marker.is_symlink(): raise ValueError('native isolation left its stale mask marker')
        row.update(phase='observed', expected=current); persist(row)
    elif action == 'restore':
        if marker.exists() or marker.is_symlink(): raise ValueError('cannot restore custom host policy before native clear')
        for path, entry in current['masks'].items():
            if entry['mask'] not in (before['masks'][path]['mask'], original['masks'][path]['mask']):
                raise ValueError('foreign CPU mask prevents original policy restoration')
        row['phase'] = 'restore-intent'; persist(row)
        order = sorted(current['masks'], key=lambda p: (
            0 if p == '/sys/devices/virtual/workqueue/cpumask' else
            1 if p == '/proc/irq/default_smp_affinity' else 2, p))
        for path in order:
            actual = root/original['masks'][path]['identity']['resolved'].lstrip('/')
            if mask(actual.read_text()) != original['masks'][path]['mask']:
                fd = os.open(actual, os.O_WRONLY|os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd); identity = original['masks'][path]['identity']
                    if (info.st_dev, info.st_ino) != (identity['device'], identity['inode']):
                        raise ValueError('CPU mask interface replaced before restore write')
                    data = original['masks'][path]['raw'].encode()
                    if os.write(fd, data) != len(data): raise ValueError('short CPU policy restoration write')
                finally: os.close(fd)
            if mask(actual.read_text()) != original['masks'][path]['mask']:
                raise ValueError('CPU policy did not restore the exact original mask')
        restored = snapshot()
        if any(restored['masks'][p]['mask'] != e['mask'] for p, e in original['masks'].items()):
            raise ValueError('original custom CPU policy restoration incomplete')
        row['restored'] = restored; row['phase'] = 'restored'; persist(row)
        # Resuming the original daemon may legitimately rebalance IRQs later.
        if original['irqbalance'].get('ActiveState') == 'active': run(['systemctl', 'start', 'irqbalance.service'])
        if service() != original['irqbalance']: raise ValueError('original irqbalance service state did not restore')
        for name, existed in sorted(row['marker_parents'].items(), key=lambda item: len(item[0]), reverse=True):
            path = root/name
            if not existed and path.exists():
                if path.is_symlink() or any(path.iterdir()): raise ValueError('foreign files in native-created marker parent')
                path.rmdir()
        record.rename(record.with_name(record.stem+'-completed.json'))
        return {'original_custom_masks_restored': True, 'mask_count': len(original['masks']),
            'before_irqbalance_resume': restored, 'irqbalance_restored': service()}
    elif action != 'capture': raise ValueError('unknown CPU isolation host action')
    return row


def remote(lab, row, node, action):
    request = row['hosts'][node]['request']
    code = inspect.getsource(host_masks)+'\nimport json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(host_masks(r["request"],r["action"]),sort_keys=True))\n'
    return json.loads(lab.remote(node, ['python3', '-c', code],
        stdin=json.dumps({'request': request, 'action': action}), timeout=180).stdout)


def save(lab, row): lab.write_private(lab.DIRECTORY/RECORD, row)


def empty(lab):
    if lab.get('volumes.longhorn.io', namespace=NS)['items']:
        raise ValueError('CPU isolation requires no native volumes')
    managers = [m for m in lab.get('instancemanagers.longhorn.io', namespace=NS)['items']
        if m['spec'].get('dataEngine') == 'v2']
    expected = {'longhorn-node-'+ip.rsplit('.', 1)[1] for ip in lab.NODES}
    if len(managers) != len(expected) or {m['spec']['nodeID'] for m in managers} != expected:
        raise ValueError('CPU isolation requires its exact source-pool V2 managers')
    if any(any(m.get('status', {}).get(key) for key in ('instanceEngines', 'instanceReplicas',
        'instanceEngineFrontends', 'instanceShards', 'instanceShardGroups', 'backingImages', 'instances')) for m in managers):
        raise ValueError('CPU isolation refuses cached native consumers')
    return managers


def managers(lab, row, global_enabled):
    result = {}; current = empty(lab)
    for manager in current:
        name = manager['metadata']['name']; saved = row['managers'].get(name)
        if not saved or manager['metadata']['uid'] != saved['uid'] or manager['spec']['image'] != saved['spec']['image']:
            raise ValueError('CPU isolation original IM identity or image changed')
        pod = lab.get('pod', name, NS)
        if (not pod or pod['metadata'].get('deletionTimestamp') or manager.get('status', {}).get('currentState') != 'running' or
                not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', []))):
            return None
        if (pod['spec']['nodeName'] != manager['spec']['nodeID'] or not any(r.get('kind') == 'InstanceManager' and
            r.get('uid') == saved['uid'] and r.get('controller') for r in pod['metadata'].get('ownerReferences', []))):
            raise ValueError('CPU isolation Pod ownership or node changed')
        override = manager['spec'].get('dataEngineSpec', {}).get('v2', {}).get('cpuIsolationEnabled', '')
        if override != row['expected_overrides'][name]: return None
        original_spec = json.loads(json.dumps(saved['spec']))
        original_spec.setdefault('dataEngineSpec', {}).setdefault('v2', {})['cpuIsolationEnabled'] = override
        actual_spec = json.loads(json.dumps(manager['spec']))
        actual_spec.setdefault('dataEngineSpec', {}).setdefault('v2', {}).setdefault('cpuIsolationEnabled', '')
        if actual_spec != original_spec:
            raise ValueError('CPU isolation changed an unrelated IM specification')
        if len(pod['spec']['containers']) != 1 or pod['spec']['containers'][0]['image'] != saved['spec']['image']:
            raise ValueError('CPU isolation Pod image changed')
        enabled = effective(global_enabled, override); args = pod['spec']['containers'][0].get('args', [])
        if any(args.count(flag) != int(enabled) for flag in FLAGS): return None
        if args.count('--longhorn-control-path') != 1 or args[args.index('--longhorn-control-path')+1] != '/var/lib/longhorn':
            raise ValueError('CPU isolation marker path differs from fixed upstream host control path')
        result[name] = {'uid': manager['metadata']['uid'], 'pod_uid': pod['metadata']['uid'],
            'node': manager['spec']['nodeID'], 'enabled': enabled, 'override': override, 'args': args}
    return result


def override(lab, row, name, value):
    if value not in ('', 'true', 'false'): raise ValueError('invalid CPU override')
    manager = next(m for m in empty(lab) if m['metadata']['name'] == name)
    if manager['metadata']['uid'] != row['managers'][name]['uid']: raise ValueError('CPU override original manager replaced')
    desired = dict(manager['spec']); desired['dataEngineSpec'] = dict(desired.get('dataEngineSpec', {}))
    desired['dataEngineSpec']['v2'] = dict(desired['dataEngineSpec'].get('v2', {}), cpuIsolationEnabled=value)
    row['override_intent'] = {'manager': name, 'value': value}; save(lab, row)
    row['expected_overrides'][name] = value; save(lab, row)
    lab.kubectl('-n', NS, 'patch', 'instancemanagers.longhorn.io', name, '--type=json',
        '--patch-file=/dev/stdin', stdin=json.dumps([
            {'op': 'test', 'path': '/metadata/uid', 'value': manager['metadata']['uid']},
            {'op': 'test', 'path': '/spec', 'value': manager['spec']},
            {'op': 'replace', 'path': '/spec', 'value': desired}]))


def apply(lab, row, relay_pid, enabled):
    path = lab.DIRECTORY/'config.yml'; config = yaml.safe_load(row['original_config'])
    config[FIELD] = enabled; row['global_intent'] = enabled; save(lab, row)
    lab.write_private(path, config)
    lab.supervise_core(relay_pid, 'longhorn-setup')
    setting = lab.get('settings.longhorn.io', SETTING, NS)
    if setting['metadata']['uid'] != row['setting']['uid'] or json.loads(setting['value']).get('v2') != str(enabled).lower():
        raise ValueError('product CPU isolation setting did not converge')


def observe(lab, row, enabled, label):
    current = lab.wait('v2-cpu-isolation-'+label, 600, lambda: managers(lab, row, enabled))
    for node, host in row['hosts'].items():
        if host.get('restored'): continue
        name = next(name for name, item in current.items() if item['node'] == 'longhorn-node-'+node.rsplit('.', 1)[1])
        action = 'enabled' if current[name]['enabled'] else 'cleared'
        # A manager that has never been enabled must preserve its original
        # custom masks, rather than being mislabeled an all-online clear.
        if action == 'cleared' and not host.get('ever_enabled'): continue
        host['observe_intent'] = action; save(lab, row)
        host['observed'] = remote(lab, row, node, action)
        if action == 'enabled': host['ever_enabled'] = True
        save(lab, row)
    result = {'stage': label, 'global': enabled, 'managers': current}
    row.setdefault('observations', []).append(result); save(lab, row)
    print('LONGHORN_V2_CPU_ISOLATION_OBSERVED '+json.dumps(result, sort_keys=True), flush=True)


def receipt(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink CPU isolation journal')
    row = json.loads(path.read_text()); state = lab.owner()
    if any(row.get(k) != state[k] for k in ('run_id', 'cluster_uid')):
        raise ValueError('foreign CPU isolation lease')
    return row


def clean(lab, relay_pid):
    path = lab.DIRECTORY/RECORD
    if not path.exists(): return
    row = receipt(lab)
    if row.get('clean'): return
    for name, original in row['managers'].items():
        override(lab, row, name, original['spec'].get('dataEngineSpec', {}).get('v2', {}).get('cpuIsolationEnabled', ''))
    apply(lab, row, relay_pid, False); observe(lab, row, False, 'cleanup-disabled')
    for node, host in row['hosts'].items():
        if not host.get('captured'): raise ValueError('uncertain host CPU acquisition requires diagnosis')
        if not host.get('restored'):
            host['restored'] = remote(lab, row, node, 'restore'); save(lab, row)
    path_config = lab.DIRECTORY/'config.yml'
    import os
    temporary = path_config.with_name('.cpu-original-'+secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(row['original_config']); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path_config)
    setting = lab.get('settings.longhorn.io', SETTING, NS)
    if {'uid': setting['metadata']['uid'], 'value': setting['value']} != row['setting']:
        raise ValueError('CPU isolation original global setting did not restore')
    lab.installation_health(); row['clean'] = True; save(lab, row)
    print('LONGHORN_V2_CPU_ISOLATION_SCOPED_CLEAN_PASS', flush=True)


def business(lab, relay_pid):
    import ast
    source = Path(__file__).resolve().parents[2]/'roles/longhorn/files/longhorn.py'
    if not any(isinstance(node, ast.FunctionDef) and node.name == 'v2_cpu_isolation'
            for node in ast.parse(source.read_text()).body):
        raise ValueError('CPU isolation requires the pinned product configuration support before host acquisition')
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink(): raise ValueError('previous CPU isolation fixture needs recovery')
    current = empty(lab); state = lab.owner(); original = (lab.DIRECTORY/'config.yml').read_text()
    config = yaml.safe_load(original)
    if config.get('longhorn_v2_cpu_cores', 0) or config.get(FIELD, False) or config['longhorn_version'] != '1.12.1':
        raise ValueError('CPU isolation gate requires its unchanged nonexclusive, disabled 1.12.1 baseline')
    setting = lab.get('settings.longhorn.io', SETTING, NS)
    if json.loads(setting['value']).get('v2') != 'false': raise ValueError('CPU isolation initial setting is not disabled')
    row = {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'], 'original_config': original,
        'setting': {'uid': setting['metadata']['uid'], 'value': setting['value']}, 'hosts': {},
        'expected_overrides': {m['metadata']['name']: '' for m in current},
        'managers': {m['metadata']['name']: {'uid': m['metadata']['uid'], 'spec': m['spec']} for m in current}}
    if any(m['spec'].get('dataEngineSpec', {}).get('v2', {}).get('cpuIsolationEnabled', '') for m in current):
        raise ValueError('CPU isolation initial per-IM overrides must be empty')
    save(lab, row)
    with lab.scoped_cleanup(lambda: clean(lab, relay_pid)):
        for node in lab.NODES:
            identity = json.loads(lab.remote(node, ['python3', '-c',
                'import json;from pathlib import Path;print(json.dumps({"machine_id":Path("/etc/machine-id").read_text().strip(),"boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip()}))']).stdout)
            if identity['machine_id'] != state['raw_disks'][node]['machine_id']:
                raise ValueError('CPU isolation host differs from its raw-disk owner')
            request = dict(identity, id=str(uuid.uuid4()), spdk_mask=config['longhorn_v2_cpu_mask'])
            row['hosts'][node] = {'request': request}; save(lab, row)
            row['hosts'][node]['captured'] = remote(lab, row, node, 'capture'); save(lab, row)
        first = sorted(row['managers'])[0]
        override(lab, row, first, 'false')
        apply(lab, row, relay_pid, True); observe(lab, row, True, 'override-false-over-global-true')
        override(lab, row, first, ''); observe(lab, row, True, 'empty-inherits-global-true')
        apply(lab, row, relay_pid, False); observe(lab, row, False, 'global-disabled-native-clear')
        override(lab, row, first, 'true'); observe(lab, row, False, 'override-true-over-global-false')
        override(lab, row, first, ''); observe(lab, row, False, 'empty-inherits-global-false')
        row['passed'] = True; save(lab, row)
    row = receipt(lab)
    if not row.get('clean') or not row.get('passed'): raise ValueError('CPU isolation did not complete scoped cleanup')
    result = {'observations': row['observations'], 'hosts': {node: host['restored'] for node, host in row['hosts'].items()},
        'config_sha256': hashlib.sha256(row['original_config'].encode()).hexdigest()}
    (lab.DIRECTORY/('longhorn-v2-cpu-isolation-completed-'+secrets.token_hex(8)+'.json')).write_text(json.dumps(result, sort_keys=True))
    path.unlink()
    print('LONGHORN_V2_CPU_ISOLATION_PASS', flush=True)


def completed_host(request, *, root='/'):
    """Read the original completed journal and present host policy; never repair."""
    import hashlib
    import json
    from pathlib import Path
    import re
    import subprocess
    root = Path(root)
    if not re.fullmatch(r'/var/lib/kubeauto-longhorn-cpu-[a-f0-9-]{36}-completed\.json', request['path']):
        raise ValueError('invalid completed CPU host receipt path')
    path = root/request['path'].lstrip('/')
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != request['sha256']:
        raise ValueError('completed CPU host receipt differs')
    row = json.loads(path.read_text()); original = row['original']; restored = row['restored']
    host = {'machine_id': (root/'etc/machine-id').read_text().strip(),
            'boot_id': (root/'proc/sys/kernel/random/boot_id').read_text().strip(),
            'online': (root/'sys/devices/system/cpu/online').read_text().strip()}
    if row['phase'] != 'restored' or host != original['host'] or host != restored['host'] or host['machine_id'] != request['machine_id']:
        raise ValueError('completed CPU host identity or restoration changed')
    marker = root/'var/lib/longhorn/instance-manager/v2/spdk_cpu_mask'
    if marker.exists() or marker.is_symlink(): raise ValueError('stale CPU isolation host marker')
    paths = [root/'proc/irq/default_smp_affinity', root/'sys/devices/virtual/workqueue/cpumask']
    paths += list((root/'proc/irq').glob('[0-9]*/smp_affinity'))
    paths += list((root/'sys/devices/virtual/workqueue').glob('*/cpumask'))
    for interface in (root/'sys/class/net').iterdir():
        if interface.is_symlink() and 'virtual' not in str(interface.readlink()):
            paths += list(interface.glob('queues/rx-*/rps_cpus'))
    if set(original['masks']) != set(restored['masks']) or {'/'+str(p.relative_to(root)) for p in paths} != set(original['masks']):
        raise ValueError('completed CPU mask topology differs')
    for name, expected in original['masks'].items():
        actual = root/name.lstrip('/'); info = actual.stat(); identity = expected['identity']
        if (info.st_dev, info.st_ino) != (identity['device'], identity['inode']) or str(actual.resolve()) != str(root/identity['resolved'].lstrip('/')):
            raise ValueError('completed CPU mask interface identity differs')
        if re.fullmatch('/proc/irq/[0-9]+/smp_affinity', name):
            irq = {key: (actual.parent/key).read_text().strip() for key in ('actions', 'chip_name', 'hwirq') if (actual.parent/key).exists()}
            if irq != identity['irq']: raise ValueError('completed CPU IRQ identity differs')
        value = int(actual.read_text().replace(',', '').strip(), 16)
        if value != expected['mask'] or restored['masks'][name]['mask'] != expected['mask'] or restored['masks'][name]['identity'] != identity:
            raise ValueError('completed CPU host mask differs from original policy')
    response = subprocess.run(['systemctl', 'show', 'irqbalance.service', '-p', 'LoadState', '-p', 'ActiveState', '-p', 'UnitFileState'],
                              capture_output=True, text=True, check=True, timeout=30)
    service = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
    if service != original['irqbalance']: raise ValueError('completed CPU irqbalance differs')
    return {'machine_id': host['machine_id'], 'mask_count': len(original['masks']), 'original_custom_masks_restored': True}


def verify_completed(lab, relay_pid, *, evidence=None):
    """Consume the original accepted CPU scope before the monitoring continuation."""
    import ast
    import re
    evidence = evidence or Path(__file__).resolve().parents[1]/'evidence/longhorn-v2-cpu-isolation-complete-20261023.json'
    if not evidence.is_file() or evidence.is_symlink(): raise ValueError('missing fixed CPU reuse evidence')
    proof = json.loads(evidence.read_text())
    if proof['status'] != 'pass' or proof.get('overall_delivery_pass') is not False:
        raise ValueError('invalid fixed CPU reuse evidence scope')
    source = Path(__file__).read_text(); nodes = ast.parse(source).body
    functions = {n.name: hashlib.sha256(ast.get_source_segment(source, n).encode()).hexdigest()
                 for n in nodes if isinstance(n, ast.FunctionDef)}
    required = {'effective', 'host_masks', 'remote', 'save', 'empty', 'managers', 'override', 'apply', 'observe', 'receipt', 'clean', 'business'}
    if set(proof['source']['functions']) != required or any(functions[name] != digest for name, digest in proof['source']['functions'].items()):
        raise ValueError('CPU owning function changed since accepted stage')
    for name, digest in proof['source']['files'].items():
        if name != 'roles/longhorn/files/longhorn.py':
            raise ValueError('CPU owning product changed since accepted stage')
        spec = importlib.util.spec_from_file_location('cpu_product_compatibility',
            Path(__file__).with_name('longhorn-im-revision-3-adopt.py'))
        revision = importlib.util.module_from_spec(spec); spec.loader.exec_module(revision)
        revision.BASE = lab.BASE
        if not revision.gate_input_current(name, digest):
            raise ValueError('CPU owning product changed since accepted stage')
    if set(proof['source']['files']) != {'roles/longhorn/files/longhorn.py'}: raise ValueError('incomplete CPU owning product binding')
    owner = lab.owner()
    if any(owner.get(name) != value for name, value in proof['owner'].items()) or set(proof['owner']) != {'run_id', 'cluster_uid', 'namespace_uid', 'node_uids', 'raw_disks'}:
        raise ValueError('CPU reuse cluster or raw-disk owner differs')
    stage = Path(proof['remote_stage']['directory'])
    if not re.fullmatch(r'/var/tmp/kubeauto-longhorn-v2-observability\.[A-Za-z0-9]+', str(stage)):
        raise ValueError('invalid CPU reuse durable directory')
    expected_files = {'cpu-isolation.log', 'cpu-isolation.exit', 'cpu-isolation.finalized'}
    if set(proof['remote_stage']['files']) != expected_files: raise ValueError('incomplete CPU durable evidence')
    for name, digest in proof['remote_stage']['files'].items():
        path = stage/name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('CPU durable evidence differs')
    if any((stage/name).read_text().strip() != '0' for name in ('cpu-isolation.exit', 'cpu-isolation.finalized')):
        raise ValueError('CPU stage did not complete both durable fences')
    lines = (stage/'cpu-isolation.log').read_text().splitlines()
    for marker in ('LONGHORN_V2_CPU_ISOLATION_PASS', 'LONGHORN_V2_CPU_ISOLATION_SCOPED_CLEAN_PASS', 'LONGHORN_V2_OBSERVABILITY_STAGE_EXIT rc=0'):
        if lines.count(marker) != 1: raise ValueError('CPU stage lacks unique success marker')
    if any(re.match(r'^(LONGHORN_[A-Z0-9_]*(FAIL|FIRST_FAILURE)( |$)|FAILED|ERROR[:!]|FAIL:)', line) for line in lines):
        raise ValueError('CPU accepted stage contains failure marker')
    name = proof['completed']['name']
    if not re.fullmatch(r'longhorn-v2-cpu-isolation-completed-[a-f0-9]{16}\.json', name): raise ValueError('invalid completed CPU receipt name')
    path = lab.DIRECTORY/name
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != proof['completed']['sha256']:
        raise ValueError('completed CPU receipt differs')
    done = json.loads(path.read_text())
    if hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest() != done['config_sha256']:
        raise ValueError('CPU reuse product configuration differs')
    for name in (RECORD, 'longhorn-v2-monitoring.json'):
        if (lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink(): raise ValueError('CPU reuse refuses active fixture residue')
    stages = ['override-false-over-global-true', 'empty-inherits-global-true', 'global-disabled-native-clear',
              'override-true-over-global-false', 'empty-inherits-global-false', 'cleanup-disabled']
    logged = [json.loads(line.split('OBSERVED ', 1)[1]) for line in lines if line.startswith('LONGHORN_V2_CPU_ISOLATION_OBSERVED ')]
    if [row['stage'] for row in done['observations']] != stages or logged != done['observations']:
        raise ValueError('incomplete CPU observation receipt')
    final = done['observations'][-1]['managers']; current = empty(lab)
    if set(final) != {m['metadata']['name'] for m in current}: raise ValueError('CPU reuse managers changed')
    for manager in current:
        saved = final[manager['metadata']['name']]; pod = lab.get('pod', manager['metadata']['name'], NS)
        if manager['metadata']['uid'] != saved['uid'] or manager['spec']['nodeID'] != saved['node'] or manager['spec'].get('dataEngineSpec', {}).get('v2', {}).get('cpuIsolationEnabled', '') or manager['status']['currentState'] != 'running':
            raise ValueError('CPU reuse IM identity or override differs')
        if len(pod['spec']['containers']) != 1 or pod['spec']['nodeName'] != saved['node'] or not any(r.get('kind') == 'InstanceManager' and r.get('uid') == saved['uid'] and r.get('controller') for r in pod['metadata'].get('ownerReferences', [])):
            raise ValueError('CPU reuse Pod ownership differs')
        if pod['spec']['containers'][0].get('args', []) != saved['args'] or not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', [])):
            raise ValueError('CPU reuse native arguments or readiness differs')
    setting = lab.get('settings.longhorn.io', SETTING, NS)
    if json.loads(setting['value']).get('v2') != 'false': raise ValueError('CPU reuse global policy differs')
    if set(proof['hosts']) != set(lab.NODES): raise ValueError('CPU reuse host inventory differs')
    lab.installation_health()
    code = inspect.getsource(completed_host)+'\nimport json,sys;print(json.dumps(completed_host(json.load(sys.stdin))))\n'
    for node, request in proof['hosts'].items():
        if request['machine_id'] != owner['raw_disks'][node]['machine_id']: raise ValueError('CPU reuse host differs from raw-disk lease')
        result = json.loads(lab.remote(node, ['python3', '-c', code], stdin=json.dumps(request), timeout=60).stdout)
        if not result['original_custom_masks_restored']: raise ValueError('CPU original host policy not restored')
    print('LONGHORN_V2_CPU_ISOLATION_REUSE_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('business', 'clean', 'verify-completed'))
    parser.add_argument('--relay-pid', type=int, required=True); args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('cpu_install', Path(__file__).with_name('longhorn-v2-install.py'))
    install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
    _, lab = install.fixture(); lab.require_lease()
    {'business': business, 'clean': clean, 'verify-completed': verify_completed}[args.action](lab, args.relay_pid)


if __name__ == '__main__': main()
