#!/usr/bin/env python3
"""Finite LONGHORN-51 composition of seven explicitly completed owning scopes.

This reader never launches a scene, scans logs, changes evidence, or signs 50.
Each supplied execution binds its original log and both durable fences into the
owning evidence's fully hashed raw closure. Business verifiers remain owned by
their original modules.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

SCOPES = ('platform', 'install', 'drain', 'interrupt', 'disk', 'worker-reboot', 'middleware')
SPECS = {
    'platform': ('v2-platform', 'longhorn-v2-runtime-resources.py'),
    'install': ('v2-install', 'longhorn-v2-coexist.py'),
    'drain': ('v2-maintenance-drain-qualification', 'longhorn-v2-maintenance-drain.py'),
    'interrupt': ('v2-interrupt', 'longhorn-v2-interrupt.py'),
    'disk': ('v2-disk-lifecycle', 'longhorn-v2-disk-lifecycle.py'),
    'worker-reboot': ('v2-failure/worker-reboot', 'longhorn-v2-runtime-faults.py'),
    'middleware': ('v2-middleware', 'longhorn-v2-middleware.py'),
}
PRODUCT = 'roles/longhorn/files/longhorn.py'
FAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', re.M)
NODES = {'longhorn-node-'+n for n in ('146', '147', '148')}


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('maintenance requires regular input: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(base, name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != name:
        raise ValueError('maintenance input escapes the explicit repository')
    target = base/path
    if base.resolve() not in target.resolve().parents:
        raise ValueError('maintenance input symlink escapes repository')
    digest(target)
    return target


def reference(base, ref):
    if set(ref) != {'path', 'sha256'} or digest(relative(base, ref['path'])) != ref['sha256']:
        raise ValueError('maintenance explicit reference SHA differs')
    return relative(base, ref['path'])


def load(base, name):
    path = base/'tests/helpers'/name
    spec = importlib.util.spec_from_file_location('maintenance51_'+name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if hasattr(module, 'BASE'): module.BASE = base
    return module


def one(lines, prefix):
    rows = [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
    if len(rows) != 1: raise ValueError('maintenance needs one actual observation: '+prefix)
    return rows[0]


def unique(lines, marker):
    if lines.count(marker) != 1: raise ValueError('maintenance needs unique owning result: '+marker)


def verify_drain(base, log):
    """Read the original eviction stderr, all fsync ACKs and application move."""
    lines = log.splitlines()
    ledger = one(lines, 'LONGHORN_MAINTENANCE_ACKNOWLEDGEMENTS ')
    control = load(base, 'longhorn-control-faults.py'); faults = load(base, 'longhorn-faults.py')
    if not ledger.get('token', '').endswith(':maintenance') or control.confirmed(ledger['records'], ledger['token'], faults) != ledger:
        raise ValueError('maintenance drain ACK sequence/checksum differs')
    refusal = one(lines, 'LONGHORN_MAINTENANCE_LAST_REPLICA_REFUSAL_PASS ')
    migration = one(lines, 'LONGHORN_MAINTENANCE_MIGRATION_VERIFIED ')
    attempts = [json.loads(line.split(' ',1)[1]) for line in lines if line.startswith('LONGHORN_MAINTENANCE_DRAIN_RESULT ')]
    denied = [r for r in attempts if r.get('refusal_expected') is True]
    allowed = [r for r in attempts if r.get('refusal_expected') is False]
    if len(denied) != 1 or len(allowed) != 1 or denied[0].get('rc') == 0 or allowed[0].get('rc') != 0:
        raise ValueError('maintenance drain lacks actual PDB refusal and successful eviction')
    message = (denied[0].get('stdout','')+'\n'+denied[0].get('stderr','')).lower()
    if 'disruption budget' not in message or not any(s in message for s in ('cannot evict', 'would violate')):
        raise ValueError('maintenance refusal is not the native disruption budget')
    replicas = refusal.get('replicas', {}); restored = refusal.get('restored', {})
    if (not refusal.get('pdb_uid') or len(replicas) != 1 or len(restored) != 3 or
            {r.get('node') for r in restored.values()} != NODES or
            len({r.get('uid') for r in restored.values() if r.get('uid')}) != 3 or
            refusal.get('node') != denied[0].get('node') or
            migration.get('node') != allowed[0].get('node') or
            migration['before'].get('node') != migration['node'] or
            migration['after'].get('node') not in NODES-{migration['node']} or
            not migration['before'].get('uid') or not migration['after'].get('uid') or
            migration['before']['uid'] == migration['after']['uid']):
        raise ValueError('maintenance drain lost the protected replica or original application move')
    for marker in ('LONGHORN_V2_MAINTENANCE_DRAIN_BUSINESS_PASS', 'LONGHORN_V2_MAINTENANCE_DRAIN_FIXTURES_CLEAN_PASS',
                   'LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_SCOPED_CLEAN_PASS',
                   'LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_PASS'):
        unique(lines, marker)
    return {'data_engine':'v2', 'ledger':ledger['summary'], 'refusal':refusal, 'migration':migration}


def verify_business(base, scope, log, artifacts):
    if FAILURE.search(log): raise ValueError('maintenance owning scope contains its first failure')
    lines = log.splitlines()
    if scope in ('platform', 'install'):
        module = load(base, 'longhorn-v2-platform-install.py')
        result = one(lines, module.SPECS[scope]['result'])
        module.validate_result(scope, result, lines)
        if scope == 'platform':
            runtime = load(base, 'longhorn-v2-runtime-resources.py')
            original = result['profiles']['original']['state']['native']['settings']
            config = {}
            for field in runtime.FIELDS:
                value = json.loads(original[field]['value'])['v2']
                config[field] = (value == 'true' if field in ('longhorn_v2_hugepages','longhorn_v2_interrupt_mode') else
                                 value if field == 'longhorn_v2_cpu_mask' else int(value))
            runtime.assert_cpu_refusals(result.get('cpu_refusals',{}),config)
            control = load(base, 'longhorn-control-faults.py'); faults = load(base, 'longhorn-faults.py')
            for name in ('anonymous-iobuf', 'exclusive', 'original'):
                profile = result['profiles'][name]; ledger = profile['ledger']
                if control.confirmed(ledger['records'], ledger['token'], faults) != ledger:
                    raise ValueError('maintenance runtime profile ACK checksum differs')
                desired = runtime.profile(config, name)
                state = profile['state']
                if len(state.get('runtime',{})) != 3 or set(state['runtime']) != set(state.get('pods',{})):
                    raise ValueError('maintenance runtime profile lacks all actual SPDK processes')
                for manager, row in state['runtime'].items():
                    runtime.assert_runtime(row, desired, state['pods'][manager])
            unique(lines, 'LONGHORN_V2_RUNTIME_ATTACHED_AND_BUDGET_REFUSAL_PASS')
        else:
            for case in ('class-engine', 'disabled-engine', 'failure-domains'):
                unique(lines, 'LONGHORN_V2_COEXIST_REFUSAL_VERIFIED case='+case)
        return result
    if scope == 'drain': return verify_drain(base, log)
    if scope == 'disk':
        if set(artifacts) != {'completed-journal'}: raise ValueError('maintenance disk needs its original completed journal')
        path = artifacts['completed-journal']
        completed = json.loads(path.read_text())
        recorded = re.findall(r'^LONGHORN_V2_DISK_LIFECYCLE_COMPLETED path=\S+ sha256=([a-f0-9]{64})$', log, re.M)
        if recorded != [digest(path)]: raise ValueError('maintenance disk completed journal is not bound to original output')
        return load(base, SPECS[scope][1]).verify_log(log, completed)
    module = load(base, SPECS[scope][1])
    if scope == 'worker-reboot': return module.verify_log(log, 'worker-reboot')
    result = module.verify_log(log)
    if scope == 'middleware':
        if (result.get('data_engine') != 'v2' or not result.get('database_uid') or
                not result.get('storage_class_uid') or len(result.get('bindings', {})) != 3 or
                len(result.get('monitoring_bindings', {})) != 6 or
                set(result.get('refusals',{})) != {'engine-disable','active-node-disable'}):
            raise ValueError('maintenance middleware omits explicit V2 database/monitoring consumers')
    return result


def validate_maintenance51(base, evidences, *, input_current=None):
    """Validate the same finite seven scopes for focused and full-run consumers."""
    base = Path(base).resolve()
    if set(evidences) != set(SCOPES): raise ValueError('maintenance requires all seven explicit completed scopes')
    results = {}; receipts = {}; cores = []
    for scope in SCOPES:
        spec = evidences[scope]
        if set(spec) != {'evidence', 'log', 'exit', 'finalized', 'artifacts'}:
            raise ValueError('maintenance explicit scope fields differ: '+scope)
        path = reference(base, spec['evidence']); row = json.loads(path.read_text())
        canonical = scope in ('platform','install') and row.get('kind') == scope
        if (row.get('status') != 'pass' or row.get('rc') != 0 or row.get('failure_markers') != 0 or
                (not canonical and (row.get('version') != '1.12.1' or row.get('signed_scope') not in ({'v2-interrupt','v2-interrupt-manager'} if scope=='interrupt' else {SPECS[scope][0]})))):
            raise ValueError('maintenance scope is not independently accepted: '+scope)
        raw = row.get('raw_logs', row.get('raw_evidence'))
        if not isinstance(raw, dict) or not raw: raise ValueError('maintenance lacks original raw closure: '+scope)
        for name, expected in raw.items():
            if digest(relative(base,name)) != expected: raise ValueError('maintenance original raw bytes changed: '+name)
        frozen = row.get('frozen_inputs', row.get('prerequisites',{}).get('inputs',{}))
        for name in (PRODUCT, 'tests/helpers/'+SPECS[scope][1]):
            if (frozen.get(name) != digest(relative(base,name)) and
                    not (name != PRODUCT and input_current is not None and input_current(name, frozen.get(name)))):
                raise ValueError('maintenance owning implementation changed: '+name)
        bound = {}
        for name in ('log', 'exit', 'finalized'):
            ref = spec[name]
            if raw.get(ref['path']) != ref['sha256']: raise ValueError('maintenance execution is outside accepted raw closure')
            bound[name] = reference(base,ref)
        for name in ('exit','finalized'):
            if bound[name].read_text().strip() != '0': raise ValueError('maintenance original durable fence is not zero: '+scope)
        if len({str(p) for p in bound.values()}) != 3: raise ValueError('maintenance durable paths alias each other')
        artifacts = {}
        for name, ref in spec['artifacts'].items():
            if raw.get(ref['path']) != ref['sha256']: raise ValueError('maintenance artifact is outside accepted raw closure')
            artifacts[name] = reference(base,ref)
        baseline = row.get('baseline_identity', {})
        baseline = baseline.get('source',baseline)
        identity = baseline.get('baseline_identity', baseline)
        run_id = baseline.get('run_id', identity.get('run_id'))
        if not run_id or not identity.get('cluster_uid') or set(identity.get('node_uids',{})) != NODES:
            raise ValueError('maintenance scope lacks original source cluster identity: '+scope)
        cores.append((run_id,identity['cluster_uid'],identity['node_uids']))
        results[scope] = verify_business(base,scope,bound['log'].read_text(),artifacts)
        receipts[scope] = {'evidence':spec['evidence'], 'execution':{k:spec[k] for k in ('log','exit','finalized')},
                           'raw_count':len(raw)}
    if any(core != cores[0] for core in cores[1:]): raise ValueError('maintenance scopes belong to different original source clusters')
    return {'status':'pass', 'signed_scope':'v2-maintenance', 'version':'1.12.1', 'data_engine':'v2',
            'rc':0, 'failure_markers':0, 'scopes':receipts, 'business':results,
            'baseline_identity':{'run_id':cores[0][0], 'cluster_uid':cores[0][1], 'node_uids':cores[0][2]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True); parser.add_argument('--evidence',type=Path,required=True)
    args = parser.parse_args(); base=args.root.resolve()
    path = args.evidence
    if not path.is_absolute(): path=base/path
    digest(path)
    if base not in path.resolve().parents: raise ValueError('maintenance explicit input is outside repository')
    value = validate_maintenance51(base,json.loads(path.read_text()))
    print('LONGHORN_V2_MAINTENANCE_RESULT '+json.dumps(value,sort_keys=True),flush=True)
    print('LONGHORN_V2_MAINTENANCE_SCOPED_CLEAN_PASS',flush=True)
    print('LONGHORN_V2_MAINTENANCE_PASS',flush=True)


if __name__ == '__main__':
    try: main()
    except (ValueError,KeyError,TypeError,OSError) as error:
        print('LONGHORN_V2_MAINTENANCE_FAIL reason='+str(error),file=sys.stderr);sys.exit(1)
