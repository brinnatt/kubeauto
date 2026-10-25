#!/usr/bin/env python3
"""Finite LONGHORN-44/46 evidence composition; no lab launcher or new receipt API.

Historical host/page/AIO scopes retain their original raw logs and identities.
Only runtime resources or coexistence are newly exercised by the fixed runner.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

BASE = Path('/usr/local/kubeauto')
HOSTS = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
HISTORICAL = ('host', 'resources', 'aio-install')
UNCHANGED = (
    'roles/longhorn/defaults/main.yml', 'roles/longhorn/files/host-check.py',
    'roles/longhorn/tasks/host.yml', 'roles/longhorn/tasks/main.yml',
    'tests/helpers/longhorn-v2-core.py', 'tests/helpers/longhorn-v2-lab.py',
    'tests/helpers/longhorn-v2-pool.py', 'tests/helpers/longhorn-v2-resources.py',
    'tests/helpers/longhorn-v2-resources.sh', 'tests/helpers/longhorn-v2-install.sh')
INSTALL_HOST_FUNCTIONS = ('host_files', 'multipath_script', 'multipath',
    'capture_hosts', 'quiesce_multipath', 'restore_hosts', 'recover_host_qualification')
BLOCK_FUNCTIONS = ('output', 'online_cpu_mask', 'cpu_policy', 'platform_receipt',
                   'inspect_disk', 'receipt')
COMPATIBLE = ('roles/longhorn/files/longhorn.py', 'roles/longhorn/files/block-check.py',
    'tests/helpers/longhorn-lab.py', 'tests/helpers/longhorn-v2-lab.py',
    'tests/helpers/longhorn-v2-core.py')
SPECS = {
    'platform': {'module': 'v2-runtime-resources',
        'result': 'LONGHORN_V2_RUNTIME_RESOURCES_RESULT ',
        'markers': ('LONGHORN_V2_RUNTIME_ATTACHED_AND_BUDGET_REFUSAL_PASS',
            'LONGHORN_V2_RUNTIME_RESOURCES_BUSINESS_PASS',
            'LONGHORN_V2_RUNTIME_RESOURCES_GATE_SCOPED_CLEAN_PASS',
            'LONGHORN_V2_RUNTIME_RESOURCES_PASS'),
        'prefix': 'LONGHORN_V2_PLATFORM',
        'files': ('tests/helpers/longhorn-v2-runtime-resources.py',
                  'tests/helpers/longhorn-v2-runtime-resources.sh')},
    'install': {'module': 'v2-coexist-qualification',
        'result': 'LONGHORN_V2_COEXIST_RESULT ',
        'markers': ('LONGHORN_V2_COEXIST_FIXTURES_CLEAN_PASS',
            'LONGHORN_V2_COEXIST_BUSINESS_PASS',
            'LONGHORN_V2_COEXIST_QUALIFICATION_SCOPED_CLEAN_PASS',
            'LONGHORN_V2_COEXIST_QUALIFICATION_PASS'),
        'prefix': 'LONGHORN_V2_INSTALL',
        'files': ('tests/helpers/longhorn-v2-coexist.py',
                  'tests/helpers/longhorn-v2-coexist.sh')}}
FAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)')


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing or symlink input: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('evidence path escapes repository')
    result = BASE/path
    if BASE.resolve() not in result.resolve().parents:
        raise ValueError('evidence symlink escapes repository')
    return result


def load(name):
    path = BASE/'tests/helpers'/name
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if hasattr(module, 'BASE'):
        module.BASE = BASE
    return module


def require_markers(lines, markers):
    if any(FAILURE.match(line) for line in lines):
        raise ValueError('failure marker in owning raw gate')
    for marker in markers:
        if lines.count(marker) != 1:
            raise ValueError('missing or duplicated owning marker: ' + marker)


def raw_named(evidence, suffix):
    matches = [name for name in evidence['raw_logs']
               if name.endswith('/'+suffix) and '/frozen-source/' not in name]
    if len(matches) != 1:
        raise ValueError('ambiguous original raw file: ' + suffix)
    return relative(matches[0])


RESOURCE_REUSE = ('tests/evidence/longhorn-v2-resources-reader-reuse-20261022.json', '029a89cc8afda74961b2753ca6e33d654379853fecab438b82b13fd4c062a746')


def reviewed_resource_input(name, original, current):
    if name != 'tests/helpers/longhorn-v2-resources.py':
        return False
    path, expected = RESOURCE_REUSE
    if digest(BASE/path) != expected:
        raise ValueError('host/page resource reuse review changed')
    review = json.loads((BASE/path).read_text())
    helper = review['resource_helper']
    if ('LONGHORN_FULL_EVIDENCE_DIR' in os.environ or
            (original, current) != (helper['old'], helper['current'])):
        raise ValueError('host/page resource helper or historical context changed')
    for key in ('mechanism_review', 'resolver', 'completed_installation_review'):
        reference = review[key]
        if digest(relative(reference['path'])) != reference['sha256']:
            raise ValueError('host/page resource reuse dependency changed: '+key)
    return True


def historical(scope):
    path = BASE/'tests/evidence'/('longhorn-v2-'+scope+'-qualification-20261020.json')
    sha = digest(path); evidence = json.loads(path.read_text())
    if (evidence.get('status') != 'pass' or evidence.get('rc') != 0 or
            evidence.get('failure_markers') != 0 or
            evidence.get('remote_input_verification', {}).get('mismatches') != 0):
        raise ValueError('historical scope is not accepted: ' + scope)
    if not evidence.get('raw_logs'):
        raise ValueError('historical scope lacks original raw logs')
    for name, expected in evidence['raw_logs'].items():
        if digest(relative(name)) != expected:
            raise ValueError('historical raw digest changed: ' + name)
    gate = raw_named(evidence, 'gate.log')
    prefix = 'LONGHORN_V2_'+scope.upper().replace('-', '_')+'_QUALIFICATION'
    require_markers(gate.read_text().splitlines(),
                    (prefix+'_SCOPED_CLEAN_PASS', prefix+'_PASS', 'LONGHORN_GATE_EXIT rc=0'))
    durable = raw_named(evidence, 'durable-status.txt').read_text()
    if (durable != evidence.get('durable_status') or
            not re.search(r'(?:^| )state=exited(?: |$)', durable) or
            not re.search(r'(?:^| )rc=0(?: |$)', durable) or
            not re.search(r'(?:^| )failure_markers=0(?: |$)', durable)):
        raise ValueError('historical durable state is not exited rc=0')
    for name in UNCHANGED:
        current, original = digest(BASE/name), evidence['frozen_inputs'].get(name)
        if current != original and not reviewed_resource_input(name, original, current):
            raise ValueError('host/page mechanism input changed: ' + name)
    frozen = gate.parent/'frozen-source'
    for name, functions in (('tests/helpers/longhorn-v2-install.py', INSTALL_HOST_FUNCTIONS),
                            ('roles/longhorn/files/block-check.py', BLOCK_FUNCTIONS)):
        original = frozen/name
        if digest(original) != evidence['frozen_inputs'][name]:
            raise ValueError('original mechanism source is unbound')
        before, after = function_asts(original), function_asts(BASE/name)
        if any(before.get(n) is None or before.get(n) != after.get(n) for n in functions):
            raise ValueError('host/page mechanism functions changed: ' + name)
    return evidence, {'path': str(path.relative_to(BASE)), 'sha256': sha,
        'raw_count': len(evidence['raw_logs']),
        'gate': str(gate.relative_to(BASE)), 'gate_sha256': digest(gate)}


def function_asts(path):
    return {node.name: ast.dump(node, include_attributes=False)
            for node in ast.parse(path.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def preflight(kind):
    proofs = {}; accepted = {}
    for scope in HISTORICAL:
        accepted[scope], proofs[scope] = historical(scope)
    aio = accepted['aio-install']; install = load('longhorn-v2-install.py')
    for name in COMPATIBLE:
        if not install.qualification_input_current(aio, name):
            raise ValueError('installation compatibility chain does not bind current input: ' + name)
    resources = json.loads(raw_named(accepted['resources'], 'restored-resource-journal.json').read_text())
    hardware = json.loads(raw_named(aio, 'native-block-receipt.json').read_text())
    if (set(resources['hosts']) != set(HOSTS) or set(hardware['nodes']) != set(HOSTS) or
            resources['cluster_uid'] != aio['baseline_identity']['cluster_uid'] or
            digest(raw_named(aio, 'native-block-receipt.json')) != aio['native_block_receipt_sha256']):
        raise ValueError('historical resource and AIO ownership differ')
    machines = {}
    for node, row in resources['hosts'].items():
        if (row['phase'] != 'restored' or
                any(row['original'][k] != row['current'][k]
                    for k in ('machine_id', 'total', 'free', 'reserved', 'kubelet_active')) or
                row['current']['total'] or row['current']['reserved'] or
                row['current']['kubelet_active'] != 'active'):
            raise ValueError('original resource restoration was incomplete')
        machines[node] = row['original']['machine_id']
    names = set(UNCHANGED + COMPATIBLE + SPECS[kind]['files'] + (
        'tests/helpers/longhorn-v2-platform-install.py', 'tests/helpers/longhorn-v2-install.py'))
    return {'kind': kind, 'historical': proofs, 'machine_ids': machines,
        'original_baseline_identity': aio['baseline_identity'],
        'original_native_block_receipt_sha256': aio['native_block_receipt_sha256'],
        'original_native_disks': hardware['nodes'],
        'inputs': {name: digest(BASE/name) for name in sorted(names)}}


def snapshot(lab):
    """Installed-owner readback; never call the empty-host assess/admission path."""
    lab.require_lease()
    lab.core_identity()
    load('longhorn-v2-install.py').volume_baseline(lab)
    state = lab.owner(); pool = load('longhorn-v2-pool.py')
    receipt = lab.DIRECTORY/'yml/longhorn/block-disks.json'
    native = json.loads(receipt.read_text()); hardware = {}
    if set(native['nodes']) != set(HOSTS):
        raise ValueError('canonical V2 scope requires its three leased raw-disk hosts')
    for address in HOSTS:
        probe = json.loads(lab.remote(address, ['python3', '-c', pool.PROBE], timeout=120).stdout)
        owned = state['raw_disks'][address]
        if (probe['machine_id'] != owned['machine_id'] or not probe['kernel_ge_6_7'] or
                probe['architecture'] not in ('x86_64', 'aarch64') or
                (probe['architecture'] == 'x86_64' and not probe['sse42_all_cpus']) or
                any(not row['loaded'] for row in probe['modules'].values()) or
                probe['memory_kib']['MemTotal'] < 3*1024**2):
            raise ValueError('current host no longer satisfies V2 platform identity/admission')
        disks = native['nodes'][address]['disks']
        if len(disks) != 1:
            raise ValueError('canonical scope requires one original native AIO disk')
        disk = next(iter(disks.values())); target = probe['by_id'].get(disk['path'])
        found = [d for d in probe['disks']['blockdevices'] if '/dev/'+d['name'] == target]
        if (len(found) != 1 or disk['disk_driver'] != 'aio' or
                disk['hardware_identity'] not in (found[0].get('wwn'), found[0].get('serial')) or
                int(found[0]['size']) != 100*1024**3 or found[0].get('ro') or
                found[0].get('children') or any(found[0].get('mountpoints', []))):
            raise ValueError('current stable raw disk differs from accepted hardware')
        hardware[address] = {'machine_id': probe['machine_id'], 'kernel': probe['kernel'],
            'architecture': probe['architecture'], 'online_cpus': probe['online_cpus'],
            'memory_total_kib': probe['memory_kib']['MemTotal'],
            'hugepages_2mi': probe['hugepages_2mi'], 'disk': disk,
            'modules_loaded': sorted(probe['modules'])}
    return {'run_id': state['run_id'],
        'baseline_identity': {k: state[k] for k in ('cluster_uid', 'namespace_uid', 'node_uids')},
        'native_block_receipt_sha256': digest(receipt), 'hardware': hardware,
        'config_sha256': digest(lab.DIRECTORY/'config.yml')}


def validate_result(kind, result, lines):
    """Pure finite 44/46 assertions, also consumed by fresh full-run producers."""
    require_markers(lines, SPECS[kind]['markers'])
    before=result.get('baseline_before');after=result.get('baseline_after')
    if (not before or before != after or set(before.get('hardware', {})) != set(HOSTS) or
            not before.get('run_id') or not before.get('config_sha256')):
        raise ValueError('owning stage identity or restoration is incomplete')
    if kind == 'platform':
        profiles = result.get('profiles', {})
        if set(profiles) != {'anonymous-iobuf', 'exclusive', 'combined', 'original'}:
            raise ValueError('runtime resource profiles incomplete')
        for name in ('anonymous-iobuf', 'exclusive', 'original'):
            if not all(profiles[name].get(k) for k in ('state', 'ledger', 'host_budget')):
                raise ValueError('runtime profile lacks actual runtime/I/O/budget')
            require_markers(lines, ('LONGHORN_V2_RUNTIME_PROFILE_PASS profile='+name,))
        combined = profiles['combined']
        if combined.get('safe_refusal') is True:
            require_markers(lines, ('LONGHORN_V2_RUNTIME_COMBINATION_SAFE_REFUSAL_PASS',))
            if not combined.get('ordinary_limits'):
                raise ValueError('combination refusal lacks actual cgroup limit evidence')
        elif not all(combined.get(k) for k in ('state', 'ledger', 'host_budget')):
            raise ValueError('combined runtime profile is unverified')
    else:
        if (result.get('idempotent') is not True or len(result.get('business', [])) != 2 or
                len(result.get('managers', [])) != 6 or
                set(result.get('disk_uuids', {})) != {'v1', 'v2'}):
            raise ValueError('dual-engine business/idempotence scope is incomplete')
        if ({row.get('engine') for row in result['business']} != {'v1', 'v2'} or
                any(not row.get('volume_uid') or not re.fullmatch(r'[0-9a-f]{64}', row.get('sha256', '')) or
                    row.get('cross_node') is not True for row in result['business']) or
                any(len(result['disk_uuids'][engine]) != 3 or
                    len([row for row in result['managers'].values() if row.get('engine') == engine]) != 3
                    for engine in ('v1', 'v2'))):
            raise ValueError('dual-engine result lacks independent cross-node data/disk proof')
    return before


def finish(kind, prior, directory):
    if preflight(kind) != prior:
        raise ValueError('canonical inputs or historical evidence changed during owning stage')
    spec = SPECS[kind]
    log_root = (BASE/'logs/longhorn-cases').resolve()
    if (directory.is_symlink() or not directory.is_dir() or
            log_root not in directory.resolve().parents or
            not (directory.name == spec['module'] or directory.name.startswith(spec['module']+'-'))):
        raise ValueError('owning stage directory is not the explicit fixed-runner result')
    for suffix in ('exit', 'finalized'):
        path = directory/('durable-'+suffix)
        digest(path)
        if path.read_text().strip() != '0':
            raise ValueError('owning stage durable '+suffix+' is not zero')
    durable = (directory/'durable-status.txt').read_text()
    if not all(re.search(r'(?:^| )'+value+r'(?: |$)', durable)
               for value in ('state=exited', 'rc=0', 'failure_markers=0')):
        raise ValueError('owning stage durable summary does not confirm clean exit')
    lines = (directory/'gate.log').read_text().splitlines()
    require_markers(lines, spec['markers']+('LONGHORN_GATE_EXIT rc=0',))
    values = [json.loads(s[len(spec['result']):]) for s in lines if s.startswith(spec['result'])]
    if len(values) != 1:
        raise ValueError('owning stage lacks one complete result')
    result = values[0]; before = result.get('baseline_before'); after = result.get('baseline_after')
    if not before or before != after:
        raise ValueError('owning stage did not restore its original baseline identity')
    if (before['baseline_identity'] != prior['original_baseline_identity'] or
            before['native_block_receipt_sha256'] != prior['original_native_block_receipt_sha256']):
        raise ValueError('historical AIO scope belongs to a different installation')
    if (set(before['hardware']) != set(HOSTS) or not before.get('run_id') or
            not before.get('config_sha256')):
        raise ValueError('owning stage identity is incomplete')
    if (directory/'baseline-before.json').exists() or (directory/'baseline-after.json').exists():
        for name, expected in (('baseline-before.json', before), ('baseline-after.json', after)):
            path=directory/name; digest(path)
            if json.loads(path.read_text()) != expected:
                raise ValueError('owning result differs from independent batch readback')
    for node, row in before['hardware'].items():
        original = prior['original_native_disks'][node]
        if (row['machine_id'] != prior['machine_ids'][node] or
                row['disk'] != next(iter(original['disks'].values()))):
            raise ValueError('historical host/resource scope belongs to different hardware')
    validate_result(kind, result, lines)
    output = {'status': 'pass', 'kind': kind, 'rc': 0, 'failure_markers': 0,
        'durable': {'exit': 0, 'finalized': 0}, 'prerequisites': prior,
        'baseline_identity': before, 'owning_result': result,
        'raw_logs': {str((directory/name).relative_to(BASE)): digest(directory/name)
            for name in ('gate.log', 'durable-status.txt', 'durable-exit', 'durable-finalized')}}
    (directory/'acceptance.json').write_text(json.dumps(output, sort_keys=True, indent=2)+'\n')
    print(spec['prefix']+'_SCOPED_CLEAN_PASS', flush=True)
    print(spec['prefix']+'_PASS', flush=True)
    return output


def verify_stage(module, directory):
    """Original durable stage plus independent before/after readback for a batch."""
    matches = [spec for spec in SPECS.values() if spec['module'] == module]
    if matches:
        markers = matches[0]['markers']
    elif module == 'v2-maintenance-drain-qualification':
        markers = ('LONGHORN_V2_MAINTENANCE_DRAIN_BUSINESS_PASS',
            'LONGHORN_V2_MAINTENANCE_DRAIN_FIXTURES_CLEAN_PASS',
            'LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_SCOPED_CLEAN_PASS',
            'LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_PASS')
    else:
        raise ValueError('unknown fixed V2 resource/installation stage')
    for suffix in ('exit', 'finalized'):
        path = directory/('durable-'+suffix); digest(path)
        if path.read_text().strip() != '0':
            raise ValueError('owning V2 stage has incomplete durable fences')
    lines=(directory/'gate.log').read_text().splitlines()
    require_markers(lines, markers+('LONGHORN_GATE_EXIT rc=0',))
    before = json.loads((directory/'baseline-before.json').read_text())
    after = json.loads((directory/'baseline-after.json').read_text())
    if not before or before != after or set(before.get('hardware', {})) != set(HOSTS):
        raise ValueError('V2 stage changed the independently read original baseline')
    if matches:
        kind=next(kind for kind,spec in SPECS.items() if spec['module']==module)
        prefix=matches[0]['result']
        values=[json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
        if len(values)!=1 or validate_result(kind,values[0],lines)!=before:
            raise ValueError('owning stage result differs from independent baseline readback')
    # This is an observed summary, produced only after reading both actual
    # durable fences and checking every raw failure/terminal marker.
    (directory/'durable-status.txt').write_text('state=exited rc=0 failure_markers=0\n')
    output = {'module': module, 'rc': 0, 'durable': {'exit': 0, 'finalized': 0},
        'baseline_before': before, 'baseline_after': after,
        'raw_logs': {name: digest(directory/name) for name in ('gate.log', 'durable-exit',
            'durable-finalized', 'baseline-before.json', 'baseline-after.json')}}
    print('LONGHORN_V2_PLATFORM_INSTALL_STAGE_RESULT '+json.dumps(output, sort_keys=True), flush=True)
    print('LONGHORN_V2_PLATFORM_INSTALL_STAGE_PASS module='+module, flush=True)
    return output


def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('preflight', 'finish', 'snapshot', 'verify-stage'))
    parser.add_argument('kind', choices=tuple(SPECS)+('v2-runtime-resources', 'v2-coexist-qualification',
                                                   'v2-maintenance-drain-qualification'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args(); BASE = args.root.resolve()
    if args.action == 'verify-stage':
        if not args.directory: parser.error('verify-stage requires --directory')
        verify_stage(args.kind, args.directory)
        return
    if not args.receipt: parser.error('this action requires --receipt')
    if args.action == 'snapshot':
        _, lab = load('longhorn-v2-install.py').fixture()
        value = snapshot(lab)
        with args.receipt.open('x') as stream:
            json.dump(value, stream, sort_keys=True); stream.write('\n')
        print('LONGHORN_V2_PLATFORM_INSTALL_BASELINE_VERIFIED', flush=True)
        return
    if args.kind not in SPECS: parser.error('canonical action requires platform or install')
    if args.action == 'preflight':
        if args.receipt.exists() or args.receipt.is_symlink():
            raise ValueError('canonical preflight receipt already exists')
        with args.receipt.open('x') as stream:
            json.dump(preflight(args.kind), stream, sort_keys=True); stream.write('\n')
        print('LONGHORN_V2_'+args.kind.upper()+'_PREREQUISITES_VERIFIED', flush=True)
    else:
        digest(args.receipt)
        if not args.directory:
            parser.error('finish requires --directory from the owning launch')
        finish(args.kind, json.loads(args.receipt.read_text()), args.directory)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        print('LONGHORN_V2_CANONICAL_FAIL reason='+str(error), file=sys.stderr)
        sys.exit(1)
