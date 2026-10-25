#!/usr/bin/env python3
"""Finite current-run material producer for the fixed Longhorn full entry point."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile

BASE = Path(__file__).resolve().parents[2]
NS = 'longhorn-system'
# Ansible's retry callback is a progress event, not a terminal task failure.
# Accept only its complete known line; durable exits and owning markers still
# decide whether the stage recovered successfully.
FAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED(?! - RETRYING: \[[^\]\r\n]+\]: .+ \([0-9]+ retries left\)\.$)|ERROR[:!]|FAIL:)')
IDENTITY = ('run_id', 'cluster_uid', 'namespace_uid', 'node_uids', 'raw_disks')
MATERIALS = {
    'v2-resources-qualification': 'v2-resources.json',
    'v2-aio-install-qualification': 'v2-install.json',
    'v2-dr-target-core': 'target-core.json',
    'v2-dr-target-install-qualification': 'target-install.json',
    'v2-backup-store-qualification': 'v2-backup-store.json',
    'v2-recurring-selector-qualification': 'v2-selector.json',
    'v2-dr-store-qualification': 'dr-store.json',
    'v2-dr-standby-qualification': 'dr-standby.json',
    'v2-dr-activation-qualification': 'dr-activation.json',
    'v2-upgrade-lifecycle': 'v2-upgrade.json',
}
# Exact fixed child names. The shell schedules actual dependencies explicitly.
MODES = '''static disabled artifacts documentation core core-smoke disk-qualification
v2-core v2-host-qualification v2-resources-qualification v2-aio-install-qualification
v2-dr-target-core v2-dr-target-install-qualification v1-operations-v2-platform-install
v2-interrupt-qualification v2-volumes v2-backup-store-qualification
v2-recurring-selector-qualification v2-dr-store-qualification
v2-dr-standby-qualification v2-dr-activation-qualification
v2-upgrade-lifecycle v2-clone v1-backup-v2-data-protection
v1-ownership-v2-linked-acceptance platform idempotence rwo rwx reclaim expansion snapshot clone
replica-scale replica-failure engine-failure
trim encryption security
network-policy observability system-restore disaster-recovery v1-upgrade-lifecycle
performance v1-stability-v2-observability v2-middleware v2-failure v2-cleanup-target
v2-disk-lifecycle v2-cleanup node-lifecycle
uninstall cleanup cleanup-qualification final-clean'''.split()
OVERRIDES = {
    'v2-runtime-resources': ('LONGHORN_V2_RUNTIME_RESOURCES_PASS','LONGHORN_V2_RUNTIME_RESOURCES_GATE_SCOPED_CLEAN_PASS'),
    'system-restore': ('LONGHORN_SYSTEM_RESTORE_GATE_PASS','LONGHORN_SYSTEM_RESTORE_GATE_SCOPED_CLEAN_PASS'),
    'node-failure': ('LONGHORN_NODE_FAILURE_GATE_PASS','LONGHORN_NODE_FAILURE_GATE_SCOPED_CLEAN_PASS'),
    'node-lifecycle': ('LONGHORN_NODE_LIFECYCLE_GATE_PASS','LONGHORN_NODE_LIFECYCLE_GATE_SCOPED_CLEAN_PASS'),
    'disk-lifecycle': ('LONGHORN_DISK_LIFECYCLE_GATE_PASS','LONGHORN_DISK_LIFECYCLE_GATE_SCOPED_CLEAN_PASS'),
    'v1-upgrade-lifecycle': ('LONGHORN_V1_UPGRADE_GATE_PASS','LONGHORN_V1_UPGRADE_GATE_SCOPED_CLEAN_PASS'),
    'v2-upgrade-lifecycle': ('LONGHORN_V2_UPGRADE_GATE_PASS','LONGHORN_V2_UPGRADE_GATE_SCOPED_CLEAN_PASS'),
    'v2-disk-lifecycle': ('LONGHORN_V2_DISK_LIFECYCLE_PASS','LONGHORN_V2_DISK_LIFECYCLE_SCOPED_CLEAN_PASS'),
    'v2-cleanup': ('LONGHORN_V2_CLEANUP_PASS','LONGHORN_V2_CLEANUP_SCOPED_CLEAN_PASS'),
    'v2-cleanup-target': ('LONGHORN_V2_TARGET_CLEANUP_PASS','LONGHORN_V2_CLEANUP_SCOPED_CLEAN_PASS'),
    'cleanup': ('LONGHORN_CLEANUP_PASS','LONGHORN_CLEANUP_SCOPED_CLEAN_PASS'),
    'cleanup-qualification': ('LONGHORN_CLEANUP_QUALIFICATION_PASS','LONGHORN_CLEANUP_QUALIFICATION_SCOPED_CLEAN_PASS'),
    'final-clean': ('LONGHORN_FULL_FINAL_CLEAN_PASS','LAB_CLEAN_VERIFY_PASS'),
}



LOCAL = ('static', 'disabled', 'artifacts', 'documentation')
# A row binds its complete original module(s), never merely a generic child exit.
CASE_STAGES = {
    1: ('static',), 2: ('disabled',), 3: ('v1-ownership-v2-linked-acceptance',),
    4: ('artifacts',), 5: ('platform',), 6: ('v1-ownership-v2-linked-acceptance',),
    7: ('idempotence',), 8: ('rwo',), 9: ('rwx',), 10: ('reclaim',),
    11: ('expansion',), 12: ('snapshot',), 13: ('clone',), 14: ('v1-backup-v2-data-protection',),
    15: ('v1-backup-v2-data-protection',), 16: ('v1-backup-v2-data-protection',), 17: ('system-restore',),
    18: ('disaster-recovery',), 19: ('replica-failure', 'v1-operations-v2-platform-install'), 20: ('v1-operations-v2-platform-install',),
    21: ('engine-failure',), 22: ('v1-operations-v2-platform-install',), 23: ('v1-operations-v2-platform-install',),
    24: ('v1-operations-v2-platform-install',), 25: ('node-lifecycle',), 26: ('v1-operations-v2-platform-install',),
    27: ('replica-scale',), 28: ('v1-operations-v2-platform-install',), 29: ('trim',), 30: ('v1-operations-v2-platform-install',),
    31: ('encryption',), 32: ('security',), 33: ('network-policy',),
    34: ('observability',), 35: ('v1-upgrade-lifecycle',),
    36: ('v1-upgrade-lifecycle',), 37: ('v1-operations-v2-platform-install',), 38: ('performance',),
    39: ('v1-stability-v2-observability',), 40: ('uninstall',),
    41: ('cleanup', 'cleanup-qualification'), 42: ('documentation',),
    44: ('v2-host-qualification', 'v2-resources-qualification', 'v1-operations-v2-platform-install'),
    45: ('v2-disk-lifecycle',),
    46: ('v2-aio-install-qualification', 'v1-operations-v2-platform-install'),
    47: ('v2-volumes',), 48: ('v1-backup-v2-data-protection',),
    49: ('v2-clone', 'v1-ownership-v2-linked-acceptance', 'v2-upgrade-lifecycle'),
    50: ('v2-failure',), 51: ('v1-operations-v2-platform-install','v2-disk-lifecycle','v2-interrupt-qualification','v2-failure','v2-middleware'),
    52: ('v2-upgrade-lifecycle',), 53: ('v1-stability-v2-observability',),
    54: ('v2-cleanup-target', 'v2-cleanup'),
}
CASE_GROUPS = '''static disabled ownership artifacts platform install idempotence rwo rwx reclaim
expansion snapshot clone backup backup-refusal recurring system-restore disaster-recovery
replica-failure node-failure engine-failure network-failure last-replica maintenance
node-lifecycle disk-lifecycle replica-scale capacity trim integrity encryption security
network-policy observability upgrade upgrade-recovery middleware performance stability
uninstall cleanup documentation delivery v2-platform v2-disk-lifecycle v2-install
v2-volumes v2-data-protection v2-clone v2-failure v2-maintenance v2-upgrade
v2-observability-performance v2-cleanup'''.split()
AGGREGATE_CASES = {43, 44, 46, 49, 51}


def require_unique(lines, marker):
    if sum(line == marker or line.startswith(marker+' ') for line in lines) != 1:
        raise ValueError('missing or duplicated original full submodule marker: '+marker)


def owning_scope(mode, lines):
    if mode in ('v1-ownership-v2-linked-acceptance', 'v1-stability-v2-observability', 'v1-operations-v2-platform-install', 'v1-backup-v2-data-protection'):
        if 'LONGHORN_PARALLEL_CHILD_RESULTS v1_rc=0 v2_rc=0' not in lines:
            raise ValueError('parallel branch did not durably complete both owning scopes')
    if mode == 'v1-ownership-v2-linked-acceptance':
        for stage in ('ownership', 'install', 'v2-linked-clone-qualification',
                      'v2-linked-lifecycle-qualification', 'v2-incomplete-clone-qualification'):
            require_unique(lines, 'LONGHORN_'+stage.upper().replace('-', '_')+'_PASS')
    if mode == 'v1-stability-v2-observability':
        for marker in ('LONGHORN_STABILITY_PASS', 'LONGHORN_V2_OBSERVABILITY_PERFORMANCE_PASS'):
            require_unique(lines, marker)
    if mode == 'v1-operations-v2-platform-install':
        for stage in ('maintenance','capacity','integrity','disk-lifecycle','node-failure',
                'network-failure','disk-failure','last-replica','middleware'):
            require_unique(lines, 'LONGHORN_PARALLEL_V1_OPERATIONS_STAGE_PASS stage='+stage+' durable_rc=0 finalized=0')
        rows=[json.loads(line.removeprefix('LONGHORN_V2_PLATFORM_INSTALL_STAGE_RESULT '))
            for line in lines if line.startswith('LONGHORN_V2_PLATFORM_INSTALL_STAGE_RESULT ')]
        modules={'v2-runtime-resources','v2-coexist-qualification','v2-maintenance-drain-qualification'}
        if len(rows)!=3 or {r.get('module') for r in rows}!=modules:
            raise ValueError('V2 platform/install batch lacks its complete three original scopes')
        for row in rows:
            if (row.get('rc')!=0 or row.get('durable')!={'exit':0,'finalized':0} or
                    not row.get('baseline_before') or row['baseline_before']!=row.get('baseline_after') or
                    not row.get('raw_logs')):
                raise ValueError('V2 platform/install batch has incomplete native readback/durable state')
            require_unique(lines,'LONGHORN_V2_PLATFORM_INSTALL_STAGE_PASS module='+row['module'])
        for kind in ('platform','install'):
            canonical=load('longhorn-v2-platform-install.py')
            canonical.validate_result(kind,observation(lines,canonical.SPECS[kind]['result']),lines)
    if mode == 'v1-backup-v2-data-protection':
        for stage in ('backup', 'backup-refusal', 'recurring'):
            require_unique(lines, 'LONGHORN_PARALLEL_V1_DATA_PROTECTION_STAGE_PASS stage='+stage+' durable_rc=0 finalized=0')
        owning_scope('v2-data-protection', lines)
    if mode == 'v1-upgrade-lifecycle':
        for marker in ('LONGHORN_UPGRADE_PASS', 'LONGHORN_UPGRADE_RECOVERY_PASS'):
            require_unique(lines, marker)
        restores=[line for line in lines if line.startswith('LONGHORN_V1_UPGRADE_ISOLATED_RESTORE_VERIFIED ')]
        if len(restores)!=4:raise ValueError('V1 full gate lacks its four isolated restore observations')
    if mode == 'v2-upgrade-lifecycle':
        require_unique(lines, 'LONGHORN_V2_UPGRADE_PASS')
        require_unique(lines, 'LONGHORN_V2_LEGACY_LINKED_REFUSALS_PASS count=4 real_upgrade_fixture=true')
    if mode == 'v2-data-protection':
        for stage in ('broken-restore', 'integrity', 'backup-refusal', 'recurring-selector',
                      'recurring-snapshot', 'recurring-backup', 'recurring-rotation',
                      'detached-backup', 'systembackup', 'systemrestore', 'dr-failback'):
            require_unique(lines, 'LONGHORN_V2_DATA_PROTECTION_STAGE_PASS stage=v2-'+stage+'-qualification')
    if mode == 'v2-failure':
        for stage in ('replica-im', 'manager', 'attached-spdk', 'remote-spdk', 'replica-network',
                      'worker-reboot', 'single-disk', 'rebuild-qos', 'all-replicas'):
            require_unique(lines, 'LONGHORN_V2_FAILURE_STAGE_PASS case='+stage+' durable_rc=0 finalized=0')


NESTED = {
    'LONGHORN_PARALLEL_DURABLE_DIRECTORY': ('kubeauto-longhorn-parallel',('v1','v2')),
    'LONGHORN_PARALLEL_V1_DURABLE_DIRECTORY': ('kubeauto-longhorn-parallel-v1',
        ('maintenance','capacity','integrity','disk-lifecycle','node-failure','network-failure',
         'disk-failure','last-replica','middleware')),
    'LONGHORN_V2_LINKED_ACCEPTANCE_DURABLE_DIRECTORY': ('kubeauto-longhorn-linked-acceptance',
        ('linked-clone','linked-lifecycle','incomplete-clone')),
    'LONGHORN_OWNERSHIP_DURABLE_DIRECTORY': ('kubeauto-longhorn-ownership',
        ('ownership-csi-refusal-qualification','ownership-preinstall-refusal-qualification','install',
         'ownership-class-refusal-qualification','ownership-controller-refusal-qualification',
         'ownership-request-refusal-qualification')),
    'LONGHORN_V2_FAILURE_DURABLE_DIRECTORY': ('kubeauto-longhorn-v2-failure',
        ('replica-im','manager','attached-spdk','remote-spdk','replica-network','worker-reboot',
         'single-disk','rebuild-qos','all-replicas')),
}


def archive_nested(mode,lines):
    work=work_directory();result=[];seen=set()
    specs={key:(stem,tuple(name+suffix for name in names for suffix in ('.log','.exit','.finalized')))
        for key,(stem,names) in NESTED.items()}
    if mode=='v1-backup-v2-data-protection':
        specs['LONGHORN_PARALLEL_V1_DURABLE_DIRECTORY']=(
            'kubeauto-longhorn-parallel-v1',tuple(name+suffix
                for name in ('backup','backup-refusal','recurring')
                for suffix in ('.log','.exit','.finalized')))
    specs['LONGHORN_V2_PLATFORM_INSTALL_DURABLE_DIRECTORY']=(
        'kubeauto-longhorn-v2-platform-install',tuple(name+'/'+suffix
            for name in ('v2-runtime-resources','v2-coexist-qualification','v2-maintenance-drain-qualification')
            for suffix in ('gate.log','owning.exit','owning.finalized','durable-status.txt',
                'baseline-before.json','baseline-after.json')))
    for label,(stem,names) in specs.items():
        matches=[line.removeprefix(label+' path=') for line in lines if line.startswith(label+' path=')]
        for value in matches:
            if value in seen:continue
            seen.add(value);original=Path(value)
            if not re.fullmatch('/var/tmp/'+re.escape(stem)+r'\.[A-Za-z0-9]+',value):
                raise ValueError('nested scope path differs from original fixed runner')
            private(original,directory=True)
            for name in names:
                source=original/name;private(source)
                if name.endswith(('.exit','.finalized')) and source.read_text().strip()!='0':
                    raise ValueError('original nested full scope lacks its actual zero durable fence')
                target=work/'raw'/mode/original.name/name
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                # Existing copies are never overwritten; every byte is the
                # original producer output, not reconstructed from markers.
                with target.open('xb') as stream:
                    stream.write(source.read_bytes());stream.flush();os.fsync(stream.fileno())
                target.chmod(0o600)
                result.append({'original':str(source),'copy':reference(target)})
    if mode == 'v2-disk-lifecycle':
        matches = re.findall(r'^LONGHORN_V2_DISK_LIFECYCLE_COMPLETED path=(\S+) sha256=([a-f0-9]{64})$', '\n'.join(lines), re.M)
        if len(matches) != 1:
            raise ValueError('full disk scope lacks its original completed journal')
        original = Path(matches[0][0]); private(original)
        if (original.parent != BASE/'clusters/longhorn-v2-gate' or
                not re.fullmatch(r'longhorn-v2-disk-lifecycle-completed-[a-f0-9-]{36}\.json', original.name) or
                digest(original) != matches[0][1]):
            raise ValueError('full disk completed journal identity changed')
        load('longhorn-v2-disk-lifecycle.py').verify_log('\n'.join(lines), json.loads(original.read_text()))
        target = work/'raw'/mode/'completed-journal.json'; target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with target.open('xb') as stream:
            stream.write(original.read_bytes());stream.flush();os.fsync(stream.fileno())
        target.chmod(0o600);result.append({'original':str(original),'copy':reference(target)})
    return result


def canonical_readback(mode, lines, *, current=None):
    kind='platform' if mode=='v2-runtime-resources' else 'install'
    canonical=load('longhorn-v2-platform-install.py')
    prefix=canonical.SPECS[kind]['result']
    result=observation(lines,prefix)
    baseline=canonical.validate_result(kind,result,lines)
    if current is None:
        _,lab=load('longhorn-v2-install.py').fixture()
        current=canonical.snapshot(lab)
    if current!=baseline:raise ValueError('current independent platform/install readback differs from restored result')
    return {'result':result,'baseline':current,'installation_healthy':True,'empty_business':True}


def accepted_stage(mode, *, inputs=None):
    work=work_directory();path=work/'accepted'/(mode+'.json');private(path)
    row=json.loads(path.read_text());refs,lines=execution(mode,work/('preflight' if mode in LOCAL else 'stages')/mode)
    if (row.get('status')!='pass' or row.get('rc')!=0 or row.get('failure_markers')!=0 or
            row.get('durable')!={'exit':0,'finalized':0} or row.get('signed_scope')!=mode or
            row.get('receipt_refs')!=refs or row.get('frozen_inputs')!=(check_inputs(work) if inputs is None else inputs)):
        raise ValueError('accepted full child no longer binds its original completed execution')
    owning_scope(mode,lines)
    for nested in row.get('nested_receipts',[]):referenced(nested['copy'])
    if mode=='v2-clone' and row.get('clone_acceptance')!=clone_acceptance():
        raise ValueError('accepted full clone no longer binds the unique canonical49 receipt')
    for proof in row.get('retirement_release_proofs',{}).values():
        for copy in proof['copies'].values():referenced(copy)
    return path,row,lines


def retirement_readback(mode,lines):
    scope='source' if mode=='v2-cleanup' else 'target'
    expected={'192.168.47.146','192.168.47.147','192.168.47.148'} if scope=='source' else {'192.168.47.149'}
    result={};work=work_directory()
    for line in lines:
        prefix='LONGHORN_V2_RELEASE_PROOF path='
        if not line.startswith(prefix):continue
        source=Path(line[len(prefix):]);private(source)
        pattern=r'/var/tmp/kubeauto-longhorn-v2-retirement\.[A-Za-z0-9]+/release-proofs/longhorn-v2-'+scope+r'-released-192\.168\.47\.\d+\.json'
        if not re.fullmatch(pattern,str(source)):raise ValueError('retirement readback path differs from original owning scope')
        row=json.loads(source.read_text());host=row.get('host')
        if (host not in expected or host in result or row.get('status')!='pass' or
                row.get('signed_scope')!='v2-'+scope+'-host-release' or
                row.get('durable')!={'exit':0,'finalized':0} or
                row.get('cleanup')!={'host_clean_verified':True,'raw_disk_clean_verified':True,'page_pool_restored':True} or
                not row.get('original_raw_disk') or not row.get('owner_run_id') or not row.get('raw_logs')):
            raise ValueError('retirement readback lacks its complete host/hardware/durable proof')
        directory=work/'raw'/mode/host;directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        copies={}
        for key,original in [('proof',source)]+[(kind,source.parent.parent/('retire.'+kind)) for kind in ('log','exit','finalized')]:
            private(original)
            if key in ('exit','finalized') and original.read_text().strip()!='0':raise ValueError('retirement original durable fence failed')
            target=directory/original.name
            with target.open('xb') as stream:
                stream.write(original.read_bytes());stream.flush();os.fsync(stream.fileno())
            target.chmod(0o600);copies[key]=reference(target)
        if row['raw_logs']!=[{'path':str(source.parent.parent/'retire.log'),'sha256':copies['log']['sha256']}]:
            raise ValueError('retirement proof differs from original inner durable raw bytes')
        result[host]={'observation':row,'copies':copies}
    if set(result)!=expected:raise ValueError('retirement did not finalize every owning host')
    return result


def release_proof(scope,host):
    mode='v2-cleanup' if scope=='source' else 'v2-cleanup-target'
    _,_,lines=accepted_stage(mode)
    pattern=r'/var/tmp/kubeauto-longhorn-v2-retirement\.[A-Za-z0-9]+/release-proofs/longhorn-v2-'+scope+'-released-'+re.escape(host)+r'\.json'
    matches=[line.removeprefix('LONGHORN_V2_RELEASE_PROOF path=') for line in lines
        if line.startswith('LONGHORN_V2_RELEASE_PROOF path=') and re.fullmatch(pattern,line.removeprefix('LONGHORN_V2_RELEASE_PROOF path='))]
    if len(matches)!=1:raise ValueError('missing unique actual finalized host release proof')
    path=Path(matches[0]);private(path);proof=json.loads(path.read_text())
    if proof.get('status')!='pass' or proof.get('signed_scope')!='v2-'+scope+'-host-release' or proof.get('host')!=host:
        raise ValueError('host release proof belongs to another retirement')
    print(str(path),flush=True)


def clone_acceptance():
    _,row,_=accepted_stage('v1-ownership-v2-linked-acceptance')
    originals=[reference for reference in row['nested_receipts']
        if re.fullmatch(r'/var/tmp/kubeauto-longhorn-parallel\.[A-Za-z0-9]+/v2\.log',reference['original'])]
    if len(originals)!=1:raise ValueError('full clone lacks unique original linked branch receipt')
    directory=referenced(originals[0]['copy']).parent
    return load('longhorn-v2-clone.py').validate_clone49(BASE,directory)


def clone_accept():
    result=clone_acceptance()
    print('LONGHORN_FULL_CLONE_ACCEPTANCE '+json.dumps(result,sort_keys=True),flush=True)
    print('LONGHORN_V2_CLONE_SCOPED_CLEAN_PASS',flush=True)
    print('LONGHORN_V2_CLONE_PASS',flush=True)


def maintenance51():
    """Bind original full receipts into the unique finite51 offline reader."""
    work = work_directory(); gate = load('longhorn-v2-maintenance.py')
    original_core = accepted_stage('v2-aio-install-qualification')[1]['baseline_identity']
    modules = {'platform':('v1-operations-v2-platform-install','v2-runtime-resources'),
        'install':('v1-operations-v2-platform-install','v2-coexist-qualification'),
        'drain':('v1-operations-v2-platform-install','v2-maintenance-drain-qualification'),
        'interrupt':('v2-interrupt-qualification',None), 'disk':('v2-disk-lifecycle',None),
        'worker-reboot':('v2-failure','worker-reboot'), 'middleware':('v2-middleware',None)}
    materials = {}
    for scope, (mode, nested) in modules.items():
        parent_path, parent, _ = accepted_stage(mode)
        refs = [*parent['receipt_refs'].values()]+[r['copy'] for r in parent['nested_receipts']]
        def select(suffix):
            matches = [ref for ref in refs if ref['path'].endswith(suffix)]
            if len(matches) != 1: raise ValueError('full maintenance lacks unique original '+scope+' '+suffix)
            referenced(matches[0]); return matches[0]
        if scope in ('platform','install','drain'):
            execution = {name:select('/'+nested+'/'+suffix) for name,suffix in
                (('log','gate.log'),('exit','owning.exit'),('finalized','owning.finalized'))}
        elif scope == 'worker-reboot':
            execution = {name:select('/'+nested+'.'+suffix) for name,suffix in
                (('log','log'),('exit','exit'),('finalized','finalized'))}
        else:
            execution = parent['receipt_refs']
        artifacts = {'completed-journal':select('/completed-journal.json')} if scope=='disk' else {}
        # This is a projection of already accepted original receipts, with the
        # original source core established by this same immutable full run.
        row = {'status':parent['status'], 'rc':parent['rc'], 'failure_markers':parent['failure_markers'],
            'version':parent['version'], 'signed_scope':gate.SPECS[scope][0],
            'frozen_inputs':parent['frozen_inputs'], 'raw_logs':{ref['path']:ref['sha256'] for ref in refs},
            'baseline_identity':original_core, 'original_parent':reference(parent_path)}
        path = work/'maintenance-material'/('scope-'+scope+'.json')
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if path.exists():
            if json.loads(path.read_text()) != row: raise ValueError('full maintenance projection changed')
        else: write(path,row)
        materials[scope] = {'evidence':reference(path), **execution, 'artifacts':artifacts}
    return gate.validate_maintenance51(BASE,materials)


def complete_result():
    import yaml
    work=work_directory();inputs=check_inputs(work)
    matrix=yaml.safe_load((BASE/'tests/longhorn-test-matrix.yaml').read_text())
    cases=matrix['cases']
    expected=['LONGHORN-%02d'%n for n in range(1,55)]
    if [row['id'] for row in cases]!=expected or [row['group'] for row in cases]!=CASE_GROUPS:
        raise ValueError('full fixed 54-case ownership contract changed')
    required=set(MODES)
    stages={mode:accepted_stage(mode,inputs=inputs) for mode in sorted(required)}
    if {path.stem for path in (work/'accepted').iterdir()}!=required:
        raise ValueError('full evidence contains missing or unknown stages')
    maintenance = maintenance51()
    materials={}
    for mode,name in MATERIALS.items():
        path=work/'evidence'/name
        if json.loads(path.read_text())!=stages[mode][1]:raise ValueError('current material differs from its owning stage')
        materials[name]=reference(path)
    rows={}
    for number,case in enumerate(cases,1):
        modes=tuple(sorted(required)) if number==43 else CASE_STAGES[number]
        if number not in AGGREGATE_CASES:
            originals=[line for mode in modes for line in stages[mode][2]]
            require_unique(originals,case['success_marker'])
        rows[case['id']]={'status':'pass','scope':case['group'],'marker':case['success_marker'],
            'stages':{mode:reference(stages[mode][0]) for mode in modes}}
    result={'schema':1,'status':'pass','signed_scope':'fresh-v1-v2-full','version':'1.12.1',
        'rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
        'frozen_inputs':inputs,'materials':materials,'maintenance_acceptance':maintenance,'cases':rows,
        'final_clean':reference(stages['final-clean'][0]),'durable_scope':'completed-child-stages','outer_durable_required':True}
    return result


def finalize():
    result=complete_result();work=work_directory()
    write(work/'full-result.json',result)
    for number in sorted(AGGREGATE_CASES-{43}):
        print(result['cases']['LONGHORN-%02d'%number]['marker'],flush=True)
    print('LONGHORN_FULL_CASES_VERIFIED count=54 fresh=true',flush=True)
    print('LONGHORN_DELIVERY_BRANCH_PASS',flush=True)
    return result


def load(name):
    spec = importlib.util.spec_from_file_location('full_'+name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def private(path, *, directory=False):
    info=Path(path).lstat(); kind=stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('full evidence requires owned private non-symlink material')


def digest(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('missing regular frozen full input')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(path):
    path=Path(path)
    if not path.resolve().is_relative_to(BASE.resolve()):raise ValueError('full receipt escapes product directory')
    private(path)
    return {'path':str(path.relative_to(BASE)), 'sha256':digest(path)}


def referenced(row):
    relative=Path(row['path'])
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('full receipt path escapes product directory')
    path=BASE/relative
    if reference(path)!=row:raise ValueError('full receipt bytes or identity changed')
    return path


def write(path, row):
    path=Path(path);private(path.parent,directory=True)
    if path.exists() or path.is_symlink():raise ValueError('full evidence is immutable and must not overwrite prior material')
    descriptor,temporary=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w') as stream:
            json.dump(row,stream,sort_keys=True,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if Path(temporary).exists():Path(temporary).unlink()


def frozen_inputs():
    explicit=('common/constants.py','conf/config.yml','controller/cluster/cli.py','service/cluster/manager.py',
        'service/cluster/downloader.py','tests/longhorn-test-matrix.yaml','tests/run_enterprise_regression.sh')
    paths=set(BASE/name for name in explicit)
    paths.add(BASE/'tests/helpers/run-durable-gate.sh')
    paths.add(BASE/'tests/helpers/lab-wipe-nodes.sh')
    paths.add(BASE/'tests/helpers/kubernetes-production-smoke.sh')
    for pattern in ('tests/helpers/longhorn*','tests/unit/test_longhorn*','tests/fixtures/longhorn*','roles/longhorn/**/*',
            'docs/storage/longhorn/*.md'):
        paths.update(path for path in BASE.glob(pattern) if path.is_file() and
            '__pycache__' not in path.parts and path.suffix not in ('.pyc','.pyo'))
    return {str(path.relative_to(BASE)):digest(path) for path in sorted(paths)}


def work_directory():
    material=load('longhorn-full-material.py');directory=material.directory(BASE)
    work=directory.parent;private(work,directory=True)
    return work


def check_inputs(work):
    path=work/'frozen-inputs.json';private(path)
    row=json.loads(path.read_text())
    if row!=frozen_inputs():raise ValueError('full frozen source inputs changed; stop current run')
    return row


def markers(mode):
    if mode not in MODES:raise ValueError('unknown full stage')
    prefix='LONGHORN_'+mode.upper().replace('-','_')
    if mode in ('static','artifacts','documentation','disabled'):
        return (prefix+'_PASS',)
    return OVERRIDES.get(mode,(prefix+'_PASS',prefix+'_SCOPED_CLEAN_PASS'))


def execution(mode,prefix):
    prefix=Path(prefix);work=work_directory()
    if prefix.parent!=work/('preflight' if mode in LOCAL else 'stages') or prefix.name!=mode:raise ValueError('stage receipts outside this exact full stage')
    refs={key:reference(Path(str(prefix)+suffix)) for key,suffix in
        (('log','.log'),('exit','.exit'),('finalized','.finalized'))}
    for key in ('exit','finalized'):
        if referenced(refs[key]).read_text().strip()!='0':raise ValueError('full child did not durably finalize with rc=0')
    lines=referenced(refs['log']).read_text().splitlines()
    if any(FAILURE.match(line) for line in lines):raise ValueError('full child raw log has a failure marker')
    for marker in markers(mode):
        if sum(line==marker or line.startswith(marker+' ') for line in lines)!=1:
            raise ValueError('full child lacks unique owning success/cleanup marker: '+marker)
    if lines.count('LONGHORN_FULL_CHILD_EXIT rc=0')!=1:raise ValueError('full child lacks its original durable exit marker')
    return refs,lines


def identity(lab, *, core=False):
    lab.core_identity();owner=lab.owner()
    keys=('run_id','cluster_uid','node_uids','raw_disks') if core else IDENTITY
    return {key:owner[key] for key in keys}


def absent(paths):
    if any(path.exists() or path.is_symlink() for path in paths):raise ValueError('full independent readback retains a journal or fixture')


def target_readback():
    install=load('longhorn-v2-dr-target-install.py');_,lab=install.fixture();lab.require_lease();install.health(lab)
    owner=identity(lab);receipt_path=lab.DIRECTORY/'yml/longhorn/block-disks.json'
    absent(lab.DIRECTORY/name for name in ('longhorn-install-business.json','longhorn-request-input.json',
        'longhorn-dr-backup-consumer.json','longhorn-v1-recovery-target.json'))
    if (lab.get('volumes.longhorn.io',namespace=NS)['items'] or
            any(obj.get('spec',{}).get('csi',{}).get('driver')=='driver.longhorn.io' for obj in lab.get('pv')['items']) or
            any(obj['metadata']['name'].startswith('longhorn-business-') for obj in lab.get('namespaces')['items'])):
        raise ValueError('target independent readback retains business')
    return lab,{'baseline_identity':owner,'native_block_receipt_sha256':digest(receipt_path),
        'config_sha256':digest(lab.DIRECTORY/'config.yml'),'installation_healthy':True,'empty_business':True}


def source_readback():
    install=load('longhorn-v2-install.py');_,lab=install.fixture();lab.require_lease()
    baseline=load('longhorn-v2-upgrade-baseline.py').capture(lab)
    absent(lab.DIRECTORY/name for name in ('longhorn-install-business.json','longhorn-v2-upgrade-preparation.json'))
    return lab,{'baseline':baseline,'installation_healthy':True,'empty_business':True,
        'install_business_journal_absent':True,'transition_journal_absent':True,
        'managers':{name:{key:row[key] for key in ('uid','node','image')} for name,row in baseline['managers'].items()}}


def backup_readback(*, both=False):
    backup=load('longhorn-v2-backups.py');install,source,backups=backup.fixture();source.require_lease()
    backup.baseline(install,source,backups)
    labs={'source':source}
    if both:
        target, _=target_readback();labs['target']=target
    clusters={}
    for side,lab in labs.items():
        owner=identity(lab);target=lab.get('backuptargets.longhorn.io','default',NS)
        resources=sum(len(lab.get(kind,namespace=NS)['items']) for kind in backups.BACKUP_RESOURCES)
        credentials=sum(bool(obj['metadata'].get('labels',{}).get('kubeauto.io/longhorn-backup-run'))
            for kind in ('pods','secrets') for obj in lab.get(kind,namespace=NS)['items'])
        if resources or credentials or lab.owner().get('relays') or not target or target['spec'].get('backupTargetURL') or target['spec'].get('credentialSecret'):
            raise ValueError('independent backup readback retains consumers or transport')
        absent(lab.DIRECTORY/name for name in ('longhorn-dr-backup-consumer.json','longhorn-v2-dr.json',
            'longhorn-dr-failback-business.json','longhorn-backup-store.json','longhorn-backup-store'))
        clusters[side]={'identity':owner,'backup_resources':resources,'backup_fixture_credentials':credentials,
            'relays':0,'volumes':len(lab.get('volumes.longhorn.io',namespace=NS)['items']),
            'backup_target_uid':target['metadata']['uid'],'config_sha256':digest(lab.DIRECTORY/'config.yml')}
        if clusters[side]['volumes']:raise ValueError('backup readback retains volumes')
    store=source.backup_store();name='longhorn-backup-'+source.owner()['run_id'].replace('-','')[:16]
    if source.execute(['docker','container','ls','-a','--filter','name=^/'+name+'$','--format','{{.ID}}']).stdout.strip():
        raise ValueError('source-owned external backup container remains')
    if store.transport_listener() is not None:raise ValueError('source TLS tunnel remains')
    listeners=source.execute(['ss','-H','-lnt']).stdout
    for port in (store.endpoints.api_port,store.endpoints.console_port):
        if re.search(r':'+str(port)+r'\s',listeners):raise ValueError('external backup API/console listener remains')
    return {'clusters':clusters,'backend_container_removed':True,'server_directory_removed':True,
        'api_console_tunnel_not_listening':True,'target_consumer_removed':True}


def observation(lines,prefix):
    rows=[json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
    if len(rows)!=1:raise ValueError('expected one exact native business observation: '+prefix)
    return rows[0]


def material(mode,row,lines):
    if mode=='v2-resources-qualification':
        core=load('longhorn-v2-core.py');_,lab=core.fixture();lab.require_lease()
        resources=load('longhorn-v2-resources.py');resources.no_storage(lab)
        absent([resources.record_path(lab)])
        hosts={node:resources.probe(lab,node) for node in lab.NODES}
        for node,current in hosts.items():
            resources.validate_host(lab.owner(),node,current)
            if current['total'] or current['free']:raise ValueError('qualification left hugepages allocated')
        row.update(baseline_identity=identity(lab,core=True),independent_readback={'hosts':hosts,'page_journal_absent':True,'core_healthy':True})
    elif mode=='v2-dr-target-core':
        target=load('longhorn-v2-dr-target.py');lab=target.fixture();lab.require_lease();owner=identity(lab,core=True)
        if lab.owner().get('target_core_smoke_sha256')!=digest(BASE/'tests/helpers/kubernetes-production-smoke.sh'):
            raise ValueError('target core lacks actual same-node product smoke')
        absent([lab.DIRECTORY/'longhorn-v2-resources.json'])
        if lab.get('namespace',NS) or lab.owner().get('relays'):raise ValueError('target core readback has storage or transport')
        row.update(baseline_identity=owner,independent_readback={'baseline_identity':owner,'longhorn_installed':False,
            'hugepage_journal':False,'relays':0,'target_core_smoke_sha256':lab.owner()['target_core_smoke_sha256']})
    elif mode=='v2-dr-target-install-qualification':
        lab,readback=target_readback()
        row.update(baseline_identity=readback['baseline_identity'],native_block_receipt_sha256=readback['native_block_receipt_sha256'],independent_readback=readback)
    elif mode in ('v2-aio-install-qualification','v2-upgrade-lifecycle'):
        lab,readback=source_readback();baseline=readback['baseline']
        row.update(baseline_identity=baseline['baseline_identity'],native_block_receipt_sha256=baseline['native_block_receipt_sha256'],
            independent_readback=readback,instance_manager_version=json.loads((BASE/'roles/longhorn/files/images.json').read_text())['1.12.1']['longhorn-instance-manager'])
        if mode=='v2-aio-install-qualification':
            prefix='LONGHORN_FSYNC_REMOUNT_VERIFIED '
            matches=[re.fullmatch(r'bytes=(\d+) sha256=([0-9a-f]{64}) replica_nodes=(.+)',line[len(prefix):]) for line in lines if line.startswith(prefix)]
            if len(matches)!=1 or not matches[0]:raise ValueError('initial installation lacks one actual fsync cross-node observation')
            row['business']=dict(zip(('bytes','sha256','replica_nodes'),matches[0].groups()))
            if (row['business']['bytes']!='9437184' or row['business']['replica_nodes']!=','.join('longhorn-node-'+a.rsplit('.',1)[1] for a in ('192.168.47.146','192.168.47.147','192.168.47.148'))):
                raise ValueError('initial installation business lacks original 9MiB payload and three independent failure domains')
        else:
            transition=observation(lines,'LONGHORN_V2_UPGRADE_BASELINE_TRANSITION ')
            validator=load('longhorn-v2-upgrade-baseline.py');after,result=validator.validate_transition(transition)
            validator.verify_execution(row,transition,result)
            if baseline!=after:raise ValueError('upgrade independent current readback differs from completed transition')
            row['transition']=transition
    else:
        both=mode.startswith('v2-dr-')
        readback=backup_readback(both=both)
        row.update(independent_readback=readback,
            baseline_identity={side:value['identity'] for side,value in readback['clusters'].items()} if both else readback['clusters']['source']['identity'])
        if mode=='v2-dr-activation-qualification':row['activated_business']=observation(lines,'LONGHORN_V2_DR_ACTIVATED_BUSINESS_VERIFIED ')
    return row


# Exact legacy .2 gate-only transitions; unknown changes remain refused.
# A fresh .3 request must bind the final merged full-reader inputs.
FULL50_CONSUMERS = {
    'tests/helpers/longhorn-v2-dr-target-install.py': (
        'af065aa3959afe9158f9ef3f766e383dc59a58753ff4b069c1dc082b635c10e3',
        '2c3e9c9b4ab6fcd79d34002f1e58c0fd0492e32d79c2d74323abc6afc0383d33'),
    'tests/helpers/longhorn-v2-install.py': (
        '49d435ef41d31e3ebb6881c4ba30752180b0ddc0c389f11a4a41dd3378152c23',
        'b6cb1ed3fd25412a0dc339a82df7662342099396f091234304f6e60d357f1343'),
    'tests/helpers/longhorn-v2-upgrade-baseline.py': (
        '4e1432f51b1719db19aa62bbfaf2da31fca5c7c81510aee967395895532b9902',
        '0b9799a2b4be605fffddec168166e716059c468256f9aa1c84b4d4a9b5817656'),
}


def focused_revision3_failure50(reader, upgrade):
    """Focused readiness only; full still executes all nine current faults."""
    transition = upgrade['transition']; previous = transition['previous_evidence']
    ref = previous['runtime_continuation']
    runtime = reader.material(ref)
    request = json.loads(reader.resolve(runtime['request']).read_text())
    if (runtime['previous_evidence'] != {key: previous[key] for key in ('path', 'sha256')} or
            request['previous_evidence']['path'] != reader.ADOPTION3 or
            any(runtime.get(key) != value for key, value in request.items() if key != 'scope')):
        raise ValueError('LONGHORN-50 revision3 readiness is not the exact request consumed by 52')
    baseline = load('longhorn-v2-upgrade-baseline.py'); baseline.BASE = BASE
    result = json.loads(reader.resolve(transition['upgrade_result']).read_text())
    baseline.verify_execution(upgrade, transition, result)
    reader.stable(transition['before'], runtime['baseline'])
    # material() verifies the real .3 adoption, finite impact and all nine
    # uniquely completed scopes with each original version/input identity.
    # Its .3 branch permits no source changes after adoption.
    print('LONGHORN_FULL_V2_FAILURE_FOCUSED_QUALIFIED scopes=9 full_module_pass=false', flush=True)


def focused_failure50(cases):
    """Reuse 52's immutable request, not a fabricated complete failure parent."""
    case = next(row for row in cases if row['id'] == 'LONGHORN-52')
    if case.get('status') != 'pass' or case.get('implementation_status') != 'implemented':
        raise ValueError('LONGHORN-50 qualification requires completed LONGHORN-52')
    name = Path(case.get('evidence', ''))
    if not str(name) or name.is_absolute() or '..' in name.parts:
        raise ValueError('LONGHORN-52 qualification reference escapes product directory')
    digest(BASE/name); upgrade = json.loads((BASE/name).read_text())
    if (upgrade.get('status') != 'pass' or upgrade.get('rc') != 0 or upgrade.get('failure_markers') != 0 or
            upgrade.get('durable') != {'exit': 0, 'finalized': 0} or
            upgrade.get('signed_scope') != 'v2-upgrade-lifecycle' or upgrade.get('version') != '1.12.1' or
            upgrade.get('remote_input_verification', {}).get('mismatches') != 0):
        raise ValueError('LONGHORN-50 qualification requires real accepted 52 evidence')
    reader = load('longhorn-v2-runtime-continuation.py'); reader.BASE = BASE
    transition = upgrade['transition']; previous = transition['previous_evidence']
    if previous.get('path') == reader.ADOPTION3:
        return focused_revision3_failure50(reader, upgrade)
    runtime = json.loads(reader.runtime_path(reader.resolve(previous['runtime_continuation'])).read_text())
    request = json.loads(reader.resolve(runtime['request']).read_text())
    if (runtime.get('schema') != 1 or runtime.get('scope') != 'focused-runtime-continuation' or
            runtime.get('fresh_installation') is not False or request.get('schema') != 1 or
            request.get('scope') != 'focused-runtime-continuation-request' or
            any(runtime.get(key) != value for key, value in request.items() if key != 'scope') or
            runtime['previous_evidence'] != {key: previous[key] for key in ('path', 'sha256')} or
            request['previous_evidence']['path'] != reader.ADOPTION):
        raise ValueError('LONGHORN-50 qualification is not the exact request consumed by 52')
    baseline = load('longhorn-v2-upgrade-baseline.py'); baseline.BASE = BASE
    result = json.loads(reader.resolve(transition['upgrade_result']).read_text())
    baseline.verify_execution(upgrade, transition, result)
    reader.stable(transition['before'], runtime['baseline'])
    prior = json.loads(reader.resolve(request['previous_evidence']).read_text())
    reader.accepted(prior, 'v2-im-revision-2-adopt-qualification')
    reader.raw_files(prior)
    pins = prior['frozen_inputs']; current = request['frozen_inputs']
    if len(pins) != 79 or set(current) != set(pins) | {reader.HELPER}:
        raise ValueError('LONGHORN-50 qualification changed the original 79-input closure')
    compatibility = json.loads(reader.resolve(request['compatibility']).read_text())
    if (compatibility.get('scope') != 'focused-runtime-continuation-gate-seams' or
            compatibility.get('product_behavior_changed') is not False or
            set(compatibility.get('changes', {})) != reader.SEAMS or
            compatibility.get('helper_sha256') != digest(BASE/reader.HELPER) or
            current[reader.HELPER] != digest(BASE/reader.HELPER)):
        raise ValueError('LONGHORN-50 qualification changed its reviewed reader or focused seams')
    for name, old in pins.items():
        selected = current[name]
        if selected != old and compatibility['changes'].get(name) != {'old_sha256': old, 'sha256': selected}:
            raise ValueError('LONGHORN-50 qualification changed an unreviewed focused input: '+name)
        actual = digest(BASE/name)
        if actual != selected and FULL50_CONSUMERS.get(name) != (selected, actual):
            raise ValueError('LONGHORN-50 qualification changed an unreviewed full input: '+name)
    scopes = request.get('failure_scopes', {})
    if set(scopes) != set(reader.SCOPES) or len({json.dumps(ref, sort_keys=True) for ref in scopes.values()}) != 9:
        raise ValueError('LONGHORN-50 qualification requires nine unique original scopes')
    for scope, ref in scopes.items():
        accepted = reader.verify_scope(ref, scope)
        if any(accepted.get('frozen_inputs', {}).get(name) != old for name, old in pins.items()):
            raise ValueError('LONGHORN-50 qualification ran against another adoption input')
    print('LONGHORN_FULL_V2_FAILURE_FOCUSED_QUALIFIED scopes=9 full_module_pass=false', flush=True)


def ready():
    import yaml
    matrix=yaml.safe_load((BASE/'tests/longhorn-test-matrix.yaml').read_text())
    if [case['id'] for case in matrix['cases']]!=['LONGHORN-%02d'%i for i in range(1,55)]:
        raise ValueError('full prerequisites require all 54 exact matrix cases')
    gaps=[]
    for case in matrix['cases']:
        if case['id']=='LONGHORN-43':continue
        if (case['id']=='LONGHORN-50' and case.get('status')=='pending' and
                case.get('implementation_status')=='implemented'):
            focused_failure50(matrix['cases']);continue
        if (case.get('status')!='pass' or case.get('implementation_status')!='implemented' or
                not case.get('evidence')):
            gaps.append(case['id']);continue
        name=Path(case['evidence'])
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('focused matrix evidence escapes product directory')
        path=BASE/name;digest(path);evidence=json.loads(path.read_text())
        if evidence.get('status')!='pass' or evidence.get('rc')!=0 or evidence.get('failure_markers')!=0:
            gaps.append(case['id'])
    if gaps:raise ValueError('full blocked by incomplete owning focused gates: '+','.join(gaps))
    print('LONGHORN_FULL_FOCUSED_PREREQUISITES_PASS cases=53',flush=True)


def local_work(path):
    path=Path(path)
    if (not path.is_absolute() or '..' in path.parts or
            not path.is_relative_to(BASE/'logs/longhorn-full') or
            not re.fullmatch(r'run\.[A-Za-z0-9-]+',path.name)):
        raise ValueError('full work must be one explicit private run inside product logs')
    current=BASE
    for part in path.relative_to(BASE).parts:
        current=current/part
        if current.is_symlink():raise ValueError('full work crosses a symlink')
    private(path,directory=True)
    return path


def full_copy_material():
    # One owning reader supplies the complete verified finite transport closure.
    material=load('longhorn-v2-clone.py').preflight_full_copy(BASE)
    refs=material.get('transport_refs',{})
    if not refs or any(not isinstance(name,str) or '\n' in name or '\r' in name or
            Path(name).is_absolute() or '..' in Path(name).parts or
            not re.fullmatch('[0-9a-f]{64}',sha) for name,sha in refs.items()):
        raise ValueError('canonical49 did not supply exact safe immutable transport refs')
    return material


def verify_full_copy_transport(work):
    path=work/'full-copy-reuse.json';private(path)
    material=full_copy_material()
    if json.loads(path.read_text())!=material:
        raise ValueError('full49 reviewed reuse closure changed during transport')
    names=work/'full-copy-files.txt';private(names)
    if names.read_text()!=''.join(name+'\n' for name in sorted(material['transport_refs'])):
        raise ValueError('full49 transport file list differs from its canonical exact refs')


def freeze_preflight(path):
    work=local_work(path)
    for name in ('preflight','evidence'):private(work/name,directory=True)
    write(work/'preflight-inputs.json',frozen_inputs())
    material=full_copy_material();write(work/'full-copy-reuse.json',material)
    descriptor=os.open(work/'full-copy-files.txt',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as stream:
        stream.write(''.join(name+'\n' for name in sorted(material['transport_refs'])))
        stream.flush();os.fsync(stream.fileno())


def verify_preflight(path):
    work=local_work(path);private(work/'preflight-inputs.json')
    verify_full_copy_transport(work)
    if json.loads((work/'preflight-inputs.json').read_text())!=frozen_inputs():
        raise ValueError('source changed during local full preflight')
    # The same finite raw/durable reader will be used after transferring bytes.
    prior=os.environ.get('LONGHORN_FULL_EVIDENCE_DIR')
    os.environ['LONGHORN_FULL_EVIDENCE_DIR']=str(work/'evidence')
    try:
        for mode in LOCAL:execution(mode,work/'preflight'/mode)
    finally:
        if prior is None:os.environ.pop('LONGHORN_FULL_EVIDENCE_DIR',None)
        else:os.environ['LONGHORN_FULL_EVIDENCE_DIR']=prior


def initialize():
    work=work_directory()
    verify_full_copy_transport(work)
    for path in (work/'stages',work/'accepted'):
        if path.exists() or path.is_symlink():raise ValueError('full starts with new private evidence directories')
        path.mkdir(mode=0o700)
    preflight=work/'preflight-inputs.json'; private(preflight)
    if json.loads(preflight.read_text())!=frozen_inputs():raise ValueError('source changed after local preflight')
    write(work/'frozen-inputs.json',frozen_inputs())
    write(work/'run.json',{'started':datetime.now(timezone.utc).isoformat(),'work':str(work),'scope':'fresh-v1-v2-full'})
    print('LONGHORN_FULL_INPUTS_FROZEN count='+str(len(frozen_inputs())),flush=True)


def accept(mode,prefix):
    work=work_directory();inputs=check_inputs(work);refs,lines=execution(mode,prefix)
    row={'schema':1,'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
        'version':'1.12.1','signed_scope':mode,'frozen_inputs':inputs,'receipt_refs':refs,
        'remote_input_verification':{'matching_files':len(inputs),'mismatches':0},'checks':list(markers(mode))}
    owning_scope(mode,lines)
    row['nested_receipts']=archive_nested(mode,lines)
    if mode=='v2-clone':
        result=observation(lines,'LONGHORN_FULL_CLONE_ACCEPTANCE ')
        if result!=clone_acceptance():raise ValueError('full clone raw receipt differs from unique canonical49 readback')
        row['clone_acceptance']=result
    if mode in ('v2-cleanup','v2-cleanup-target'):
        row['retirement_release_proofs']=retirement_readback(mode,lines)
    if mode=='v1-operations-v2-platform-install':
        _,lab=load('longhorn-v2-install.py').fixture()
        current=load('longhorn-v2-platform-install.py').snapshot(lab)
        row['independent_readback']={name:canonical_readback(name,lines,current=current) for name in ('v2-runtime-resources','v2-coexist-qualification')}
    if mode in MATERIALS:
        row=material(mode,row,lines)
        # Current files are written once, only after native independent reads.
        write(work/'evidence'/MATERIALS[mode],row)
    write(work/'accepted'/(mode+'.json'),row)
    print('LONGHORN_FULL_STAGE_ACCEPTED stage='+mode+' durable_rc=0 finalized=0',flush=True)
    return row


def import_result(directory,outer):
    work=local_work(directory);outer=Path(outer)
    if (not outer.is_relative_to(BASE/'logs/longhorn-cases') or outer.is_symlink() or
            not outer.is_dir() or not outer.name.startswith('full-')):
        raise ValueError('full import requires its original explicit outer runner directory')
    for path in (outer,work):private(path,directory=True)
    refs={key:reference(outer/name) for key,name in
        (('log','gate.log'),('exit','durable-exit'),('finalized','durable-finalized'),('status','durable-status.txt'))}
    for key in ('exit','finalized'):
        if referenced(refs[key]).read_text().strip()!='0':raise ValueError('outer full runner not finalized rc=0')
    durable=referenced(refs['status']).read_text()
    if not all(re.search(r'(?:^| )'+value+r'(?: |$)',durable)
            for value in ('state=exited','rc=0','failure_markers=0')):
        raise ValueError('outer full durable summary disagrees with original fences')
    lines=referenced(refs['log']).read_text().splitlines()
    if any(FAILURE.match(line) for line in lines):raise ValueError('outer full log contains a first failure')
    if 'LAB_CLEAN_VERIFY_PASS' not in lines:raise ValueError('full log lacks original final clean verification')
    for marker in ('LONGHORN_DELIVERY_BRANCH_PASS','LONGHORN_GATE_EXIT rc=0',
            'LONGHORN_FULL_CASES_VERIFIED count=54 fresh=true'):
        require_unique(lines,marker)
    previous=os.environ.get('LONGHORN_FULL_EVIDENCE_DIR')
    os.environ['LONGHORN_FULL_EVIDENCE_DIR']=str(work/'evidence')
    try:
        result=complete_result();path=work/'full-result.json'
        if json.loads(path.read_text())!=result:raise ValueError('full result differs from current original stages')
        accepted={'schema':1,'status':'pass','signed_scope':'fresh-v1-v2-full',
            'version':'1.12.1','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
            'frozen_inputs':result['frozen_inputs'],'cases':result['cases'],
            'full_result':reference(path),'receipt_refs':refs,'outer_durable_required':False}
        write(work/'accepted-full.json',accepted)
    finally:
        if previous is None:os.environ.pop('LONGHORN_FULL_EVIDENCE_DIR',None)
        else:os.environ['LONGHORN_FULL_EVIDENCE_DIR']=previous
    print('LONGHORN_FULL_IMPORT_PASS cases=54 durable_rc=0 finalized=0',flush=True)
    return accepted


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True);sub.add_parser('init');sub.add_parser('check-inputs');sub.add_parser('finalize');sub.add_parser('ready');sub.add_parser('clone-accept')
    proof=sub.add_parser('release-proof');proof.add_argument('scope',choices=('source','target'));proof.add_argument('host',choices=('192.168.47.148','192.168.47.149'))
    stage=sub.add_parser('accept');stage.add_argument('mode',choices=MODES);stage.add_argument('--prefix',type=Path,required=True)
    for command in ('freeze','verify-preflight'):
        local=sub.add_parser(command);local.add_argument('--directory',type=Path,required=True)
    imported=sub.add_parser('import-result');imported.add_argument('--directory',type=Path,required=True);imported.add_argument('--outer-directory',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='ready':ready();return
    if args.command=='freeze':freeze_preflight(args.directory);return
    if args.command=='verify-preflight':verify_preflight(args.directory);return
    if args.command=='import-result':import_result(args.directory,args.outer_directory);return
    load('longhorn-lab.py').require_lease()
    if args.command=='init':initialize()
    elif args.command=='check-inputs':check_inputs(work_directory())
    elif args.command=='finalize':finalize()
    elif args.command=='clone-accept':clone_accept()
    elif args.command=='release-proof':release_proof(args.scope,args.host)
    else:accept(args.mode,args.prefix)


if __name__=='__main__':
    try:main()
    except Exception as error:
        import traceback
        with tempfile.NamedTemporaryFile('w',prefix='longhorn-full-evidence-failure-',suffix='.txt',dir='/var/tmp',delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_FULL_EVIDENCE_FAIL reason='+type(error).__name__+' private_diagnostic='+diagnostic.name,flush=True)
        raise SystemExit(1)
