#!/usr/bin/env python3
"""Read an explicit reviewed identity continuation; never sign installation GA."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

BASE = Path('/usr/local/kubeauto')
ENV = 'LONGHORN_V2_RUNTIME_CONTINUATION'
SCOPES = ('replica-im', 'manager', 'attached-spdk', 'remote-spdk', 'replica-network',
          'worker-reboot', 'single-disk', 'rebuild-qos', 'all-replicas')
# Only the explicitly reviewed gate seams may differ from the accepted .2 pins.
SEAMS = {'tests/helpers/longhorn-v2-upgrade-baseline.py', 'tests/helpers/longhorn-v2-install.py'}
HELPER = 'tests/helpers/longhorn-v2-runtime-continuation.py'
ADOPTION = 'tests/evidence/longhorn-v2-im-revision-2-adopt-qualification-20261021.json'
ADOPTION3 = 'tests/evidence/longhorn-v2-im-revision-3-adopt-qualification-20261022.json'
FAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)')


def load(name):
    spec = importlib.util.spec_from_file_location('runtime_continuation_'+name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.BASE = BASE
    return module


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('continuation requires regular non-symlink files')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(path):
    path = Path(path)
    return {'path': str(path.relative_to(BASE)), 'sha256': digest(path)}


def resolve(ref):
    path = Path(ref['path'])
    if path.is_absolute() or '..' in path.parts or not (BASE/path).resolve().is_relative_to(BASE.resolve()):
        raise ValueError('continuation reference escapes product directory')
    if digest(BASE/path) != ref['sha256']: raise ValueError('continuation referenced bytes changed')
    return BASE/path


def active():
    return ENV in os.environ


def runtime_path(selected=None):
    path = Path(os.environ[ENV] if selected is None else selected)
    if not path.is_absolute(): path = BASE/path
    logs = BASE/'logs'
    if (not path.resolve().is_relative_to(logs.resolve()) or path.is_symlink() or
            path.parent.is_symlink() or path.parent.stat().st_uid != os.geteuid() or
            path.parent.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077):
        raise ValueError('continuation must be a private owned runtime material below logs')
    digest(path)
    return path


def accepted(row, scope, *, tag='v1.12.1-kubeauto.2'):
    if (row.get('status') != 'pass' or row.get('rc') != 0 or row.get('failure_markers') != 0 or
            row.get('durable') != {'exit': 0, 'finalized': 0} or row.get('signed_scope') != scope or
            row.get('version') != '1.12.1' or row.get('instance_manager_version') != tag or
            row.get('remote_input_verification', {}).get('mismatches') != 0):
        raise ValueError('continuation requires a real completed named acceptance scope')


def raw_files(row):
    refs = row.get('raw_evidence', {})
    if not refs: raise ValueError('continuation scope lacks original raw evidence')
    return {name: resolve({'path': name, 'sha256': sha}) for name, sha in refs.items()}


def verify_scope(ref, scope, *, tag='v1.12.1-kubeauto.2'):
    row = json.loads(resolve(ref).read_text()); accepted(row, 'v2-failure/'+scope, tag=tag)
    files = raw_files(row)
    matches = [path for name, path in files.items() if name.endswith('/'+scope+'.log')]
    if len(matches) != 1: raise ValueError('continuation requires the unique original owning scope log')
    path = matches[0]
    for suffix in ('.exit', '.finalized'):
        fence = path.with_suffix(suffix)
        if str(fence.relative_to(BASE)) not in files or fence.read_text().strip() != '0':
            raise ValueError('continuation scope lacks actual durable zero fences')
    log = path.read_text(); lines = log.splitlines()
    if any(FAILURE.match(line) for line in lines) or lines.count('LONGHORN_V2_FAILURE_STAGE_EXIT rc=0') != 1:
        raise ValueError('continuation scope log has failures or no exact durable marker')
    if scope == 'replica-im': helper, args = 'longhorn-v2-control-faults.py', ()
    elif scope == 'manager': helper, args = 'longhorn-v2-manager-loss.py', ()
    elif scope == 'all-replicas': helper, args = 'longhorn-v2-all-replicas.py', ()
    elif scope in ('single-disk', 'rebuild-qos'): helper, args = 'longhorn-v2-disk-faults.py', (scope,)
    else: helper, args = 'longhorn-v2-runtime-faults.py', (scope,)
    load(helper).verify_log(log, *args)
    return row


def stable(current, baseline):
    for key in ('baseline_identity', 'native_block_receipt_sha256', 'native_block_receipt', 'config_sha256', 'retained'):
        if key not in current or current[key] != baseline.get(key):
            raise ValueError('continuation immutable core/native disk/receipt/config ownership changed')
    expected = {'longhorn-node-'+str(n) for n in (146, 147, 148)}
    def runtimes(rows):
        if (len(rows) != 3 or {r.get('node') for r in rows.values()} != expected or
                any(not r.get('uid') or not r.get('pod_uid') or not r.get('image', '').endswith((':v1.12.1-kubeauto.2', ':v1.12.1-kubeauto.3')) or
                    not re.search(r'sha256:[a-f0-9]{64}$', r.get('image_id', '')) for r in rows.values()) or
                any(len({r[k] for r in rows.values()}) != 3 for k in ('uid', 'pod_uid'))):
            raise ValueError('continuation requires three unique healthy pinned native owners')
        return {r['node']: (r['image'], r['image_id']) for r in rows.values()}
    if runtimes(current.get('managers', {})) != runtimes(baseline.get('managers', {})):
        raise ValueError('continuation runtime image/digest changed')
    return current


def read_inputs(row):
    if row.get('previous_evidence', {}).get('path') == ADOPTION3:
        return read_revision3_inputs(row)
    prior = json.loads(resolve(row['previous_evidence']).read_text())
    if row['previous_evidence']['path'] != ADOPTION: raise ValueError('continuation changed owning adoption')
    accepted(prior, 'v2-im-revision-2-adopt-qualification'); files = raw_files(prior)
    adoption_logs = [path for name, path in files.items() if name.endswith('/v1.log')]
    if len(adoption_logs) != 1: raise ValueError('continuation lacks original adoption owning log')
    for suffix in ('.exit', '.finalized'):
        fence = adoption_logs[0].with_suffix(suffix)
        if str(fence.relative_to(BASE)) not in files or fence.read_text().strip() != '0':
            raise ValueError('continuation adoption lacks original durable fences')
    lines = adoption_logs[0].read_text().splitlines()
    markers = ('LONGHORN_V2_IM_REVISION_2_ADOPT_BUSINESS_PASS',
        'LONGHORN_V2_IM_REVISION_2_ADOPT_FIXTURES_CLEAN_PASS',
        'LONGHORN_V2_IM_REVISION_2_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS',
        'LONGHORN_V2_IM_REVISION_2_ADOPT_QUALIFICATION_PASS')
    if any(FAILURE.match(line) for line in lines) or any(lines.count(marker) != 1 for marker in markers):
        raise ValueError('continuation adoption original log lacks exact success/cleanup markers')
    readback = prior['independent_readback']
    if (not all(readback.get(k) is True for k in ('installation_healthy', 'empty_business', 'adoption_journal_absent')) or
            readback['baseline']['baseline_identity'] != prior['baseline_identity']):
        raise ValueError('continuation historical native projection changed')
    compatibility = json.loads(resolve(row['compatibility']).read_text())
    changes = compatibility.get('changes', {})
    if (compatibility.get('scope') != 'focused-runtime-continuation-gate-seams' or
            not changes or not set(changes) <= SEAMS or compatibility.get('product_behavior_changed') is not False):
        raise ValueError('continuation compatibility exceeds reviewed exact gate seams')
    contract = compatibility['contract_receipts']
    for name in ('exit', 'finalized'):
        if resolve(contract[name]).read_text().strip() != '0': raise ValueError('continuation contract did not finalize zero')
    contract_lines = resolve(contract['log']).read_text().splitlines()
    if (contract_lines.count('LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_PASS') != 1 or
            any(FAILURE.match(line) for line in contract_lines)):
        raise ValueError('continuation requires exact green compatibility contract')
    prefix = 'LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_INPUTS '
    pinned = [{**{name: change['sha256'] for name, change in changes.items()}, HELPER: compatibility['helper_sha256']}]
    if [json.loads(line[len(prefix):]) for line in contract_lines if line.startswith(prefix)] != pinned:
        raise ValueError('continuation contract was not run against these exact reviewed seam bytes')
    current = row['frozen_inputs']
    if set(current) != set(prior['frozen_inputs']) | {HELPER}: raise ValueError('continuation frozen input domain changed')
    for name, old in prior['frozen_inputs'].items():
        actual = digest(BASE/name)
        if current.get(name) != actual: raise ValueError('continuation current input drift: '+name)
        if actual != old and changes.get(name) != {'old_sha256': old, 'sha256': actual}:
            raise ValueError('continuation lacks exact reviewed old/new gate seam: '+name)
        if actual == old and name in changes: raise ValueError('continuation compatibility declares a false input change')
    if current[HELPER] != digest(BASE/HELPER) or compatibility.get('helper_sha256') != current[HELPER]:
        raise ValueError('continuation reader bytes differ from reviewed contract')
    scopes = row.get('failure_scopes', {})
    if set(scopes) != set(SCOPES) or len({json.dumps(v, sort_keys=True) for v in scopes.values()}) != 9:
        raise ValueError('continuation requires all nine uniquely accepted failure scopes')
    for scope, ref in scopes.items():
        failure = verify_scope(ref, scope)
        for name, old in prior['frozen_inputs'].items():
            if failure.get('frozen_inputs', {}).get(name) != old:
                raise ValueError('continuation failure scope ran against another adoption input')
    return prior


ROUTING_SEAMS = ('tests/helpers/longhorn-runner.sh',
                 'tests/helpers/longhorn-im-revision-3-adopt.py',
                 'tests/helpers/longhorn-v2-runtime-continuation.py')


def read_revision3_inputs(row):
    """Use the final .3 frozen tree and an explicit finite historical edge."""
    if row['previous_evidence']['path'] != ADOPTION3:
        raise ValueError('revision3 continuation changed owning adoption')
    prior = json.loads(resolve(row['previous_evidence']).read_text())
    accepted(prior, 'v2-im-revision-3-adopt-qualification', tag='v1.12.1-kubeauto.3')
    compatibility = json.loads(resolve(row['compatibility']).read_text())
    changes = compatibility.get('changes')
    if (compatibility.get('scope') != 'focused-runtime-continuation-revision3' or
            compatibility.get('product_behavior_changed') is not True or not isinstance(changes, dict) or
            set(changes) not in (set(), set(ROUTING_SEAMS))):
        raise ValueError('revision3 continuation cannot hide product changes or unreviewed routing seams')
    revision = load('longhorn-im-revision-3-adopt.py')
    impact, _ = revision.read_reuse_impact(compatibility['reuse_impact'])
    if impact['adoption_evidence'] != row['previous_evidence']:
        raise ValueError('revision3 continuation impact refers to another adoption')
    readback = prior['independent_readback']
    if (not all(readback.get(key) is True for key in ('installation_healthy', 'empty_business', 'adoption_journal_absent')) or
            readback['baseline']['baseline_identity'] != prior['baseline_identity']):
        raise ValueError('revision3 continuation lost its actual adoption projection')
    files = raw_files(prior)
    receipts = prior.get('receipt_refs', {})
    if set(receipts) != {'log', 'exit', 'finalized'}:
        raise ValueError('revision3 continuation requires explicit original adoption execution receipts')
    for key, ref in receipts.items():
        path = resolve(ref)
        if str(path.relative_to(BASE)) not in files or prior['raw_evidence'][ref['path']] != ref['sha256']:
            raise ValueError('revision3 adoption execution receipt is outside its frozen raw evidence')
        if key != 'log' and path.read_text().strip() != '0':
            raise ValueError('revision3 adoption original execution was not finalized zero')
    log = resolve(receipts['log']).read_text(); lines = log.splitlines()
    required = ('LONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS',
                'LONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_PASS')
    if any(FAILURE.match(line) for line in lines) or any(lines.count(marker) != 1 for marker in required):
        raise ValueError('revision3 adoption original execution lacks exact scoped-clean/pass markers')
    revision.profile().verify_log(log, 'v2', historical=True)
    current = row['frozen_inputs']
    if set(current) != set(prior['frozen_inputs']) | {HELPER}:
        raise ValueError('revision3 continuation frozen input domain differs from the final adoption')
    for name, sha in current.items():
        change = changes.get(name)
        if change is not None:
            if (set(change) != {'old_sha256', 'sha256'} or
                    change['old_sha256'] != prior['frozen_inputs'].get(name) or change['sha256'] != sha):
                raise ValueError('revision3 routing seam lacks its exact adoption/current edge: '+name)
        if (not revision.gate_input_current(name, sha) or
                (name != HELPER and change is None and sha != prior['frozen_inputs'][name])):
            raise ValueError('revision3 continuation input changed after adoption: '+name)
    if compatibility.get('helper_sha256') != current[HELPER]:
        raise ValueError('revision3 continuation reader differs from its reviewed receipt')
    contract = compatibility['contract_receipts']
    for name in ('exit', 'finalized'):
        if resolve(contract[name]).read_text().strip() != '0':
            raise ValueError('revision3 continuation contract did not finalize zero')
    lines = resolve(contract['log']).read_text().splitlines()
    prefix = 'LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_INPUTS '
    if (lines.count('LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_PASS') != 1 or any(FAILURE.match(line) for line in lines) or
            [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)] != [{HELPER: current[HELPER], **{name: change['sha256'] for name, change in changes.items()}}]):
        raise ValueError('revision3 continuation lacks its exact green current-reader contract')
    scopes = row.get('failure_scopes', {})
    if set(scopes) != set(SCOPES) or len({json.dumps(ref, sort_keys=True) for ref in scopes.values()}) != 9:
        raise ValueError('revision3 continuation requires nine unique original fault scopes')
    previous = json.loads(resolve(impact['previous_evidence']).read_text())
    for scope, ref in scopes.items():
        legacy = scope in impact['legacy_failure_scopes']
        if legacy and ref != impact['legacy_failure_scopes'][scope]:
            raise ValueError('revision3 continuation changed the reviewed historical scope reference')
        if scope == 'single-disk' and ref != impact['single_disk']:
            raise ValueError('revision3 continuation changed the actual .3 single-disk proof')
        failure = verify_scope(ref, scope, tag='v1.12.1-kubeauto.2' if legacy else 'v1.12.1-kubeauto.3')
        pins = previous['frozen_inputs'] if legacy else prior['frozen_inputs']
        if any(failure.get('frozen_inputs', {}).get(name) != sha for name, sha in pins.items()):
            raise ValueError('revision3 continuation fault has another exact input identity')
    return prior


def material(ref=None):
    path = runtime_path(resolve(ref) if ref is not None else None)
    row = json.loads(path.read_text())
    if row.get('schema') != 1 or row.get('scope') != 'focused-runtime-continuation' or row.get('fresh_installation') is not False:
        raise ValueError('continuation material scope differs; no fresh/adoption/full inference')
    prior = read_inputs(row); readback = prior['independent_readback']
    if 'request' in row:
        request_row = json.loads(resolve(row['request']).read_text())
        if (request_row.get('scope') != 'focused-runtime-continuation-request' or
                any(row.get(key) != value for key, value in request_row.items() if key != 'scope')):
            raise ValueError('continuation differs from the exact local request')
    if row.get('adoption_projection') != readback['baseline']:
        raise ValueError('continuation original adoption projection changed')
    baseline = row['baseline']; stable(baseline, baseline)
    publication = json.loads(resolve(prior['transition']['publication_evidence']).read_text())
    record = publication.get('publication_record', {})
    if (publication.get('status') != 'pass' or publication.get('durable_rc') != 0 or
            publication.get('failure_markers') != 0 or publication.get('selected_image_count') != 1 or
            record.get('key') != 'brinnatt/longhorn-instance-manager:'+prior['instance_manager_version'] or
            set(record.get('platforms', {})) != {'linux/amd64', 'linux/arm64'}):
        raise ValueError('continuation requires exact dual-architecture pinned publication evidence')
    allowed = {record.get('digest')} | set(record['platforms'].values())
    if any(re.search(r'sha256:[a-f0-9]{64}$', m['image_id']).group() not in allowed
            for m in baseline['managers'].values()):
        raise ValueError('continuation current runtime digest was not published')
    projection = row['adoption_projection']; identity = baseline['baseline_identity']
    disks = {r['name']+'/'+name: d['disk_uuid'] for r in baseline['native_block_receipt']['nodes'].values() for name,d in r['disks'].items()}
    if (any(identity.get(k) != v for k,v in prior['baseline_identity'].items()) or
            baseline['config_sha256'] != projection['config_sha256'] or
            baseline['native_block_receipt_sha256'] != projection['native_block_receipt_sha256'] or
            disks != projection['disk_uuids'] or
            {r['node']: r['image'] for r in baseline['managers'].values()} !=
            {r['node']: r['image'] for r in readback['managers'].values()}):
        raise ValueError('continuation changes accepted physical/native/configuration identity')
    return row


def bind_before(before, ref=None):
    if ref is None: ref = reference(runtime_path())
    row = material(ref); stable(before, row['baseline'])
    return {**row['previous_evidence'], 'runtime_continuation': ref}


def preflight(lab):
    row = material()
    records = ('longhorn-install-business.json', 'longhorn-encryption-key.json',
               'longhorn-request-input.json', 'longhorn-reclaim-class.json', 'longhorn-v2-upgrade-preparation.json')
    if any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in records):
        raise ValueError('continuation live installation retains an owned operation journal')
    baseline = load('longhorn-v2-upgrade-baseline.py').capture(lab)
    stable(baseline, row['baseline'])
    projection = load('longhorn-v2-im-adopt.py').baseline(lab, {
        'baseline_identity': row['adoption_projection']['baseline_identity'],
        'native_block_receipt_sha256': row['baseline']['native_block_receipt_sha256']})
    if projection != row['adoption_projection']: raise ValueError('continuation live adoption projection changed')
    print('LONGHORN_V2_RUNTIME_CONTINUATION_VERIFIED scopes=9 physical_identity=unchanged ephemeral_uids=allowed', flush=True)
    return baseline


def save_private(path, row):
    path = Path(path)
    if path.exists() or path.is_symlink(): raise ValueError('continuation material is immutable')
    if (not path.resolve().is_relative_to((BASE/'logs').resolve()) or path.parent.is_symlink() or
            path.parent.stat().st_uid != os.geteuid() or path.parent.stat().st_mode & 0o077):
        raise ValueError('continuation output requires a private owned runtime directory')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream: json.dump(row, stream, sort_keys=True); stream.write('\n')


def request(compatibility, scopes):
    import tempfile
    catalog = json.loads((BASE/'roles/longhorn/files/images.json').read_text())
    owning = ADOPTION3 if catalog['1.12.1']['longhorn-instance-manager'] == 'v1.12.1-kubeauto.3' else ADOPTION
    prior = json.loads((BASE/owning).read_text())
    selected = {}
    for name in scopes:
        path = Path(name)
        if not path.is_absolute(): path = BASE/path
        scope = json.loads(path.read_text()).get('signed_scope', '').removeprefix('v2-failure/')
        if scope in selected: raise ValueError('duplicate continuation failure scope')
        selected[scope] = reference(path)
    compat = Path(compatibility)
    if not compat.is_absolute(): compat = BASE/compat
    row = {'schema': 1, 'scope': 'focused-runtime-continuation-request',
        'previous_evidence': reference(BASE/owning), 'compatibility': reference(compat), 'failure_scopes': selected,
        'frozen_inputs': {name: digest(BASE/name) for name in set(prior['frozen_inputs']) | {HELPER}}}
    read_inputs(row)
    directory = Path(tempfile.mkdtemp(prefix='v2-continuation.', dir=BASE/'logs/longhorn-cases'))
    path = directory/'request.json'; save_private(path, row)
    print(str(path.relative_to(BASE)), flush=True)
    return path


def read_request(path):
    path = Path(path)
    if not path.is_absolute(): path = BASE/path
    relative = str(path.relative_to(BASE))
    if not re.fullmatch(r'logs/longhorn-cases/v2-continuation\.[A-Za-z0-9_]+/request.json', relative):
        raise ValueError('continuation request is outside the fixed runtime path')
    digest(path); row = json.loads(path.read_text())
    if row.get('schema') != 1 or row.get('scope') != 'focused-runtime-continuation-request':
        raise ValueError('invalid explicit continuation request')
    read_inputs(row)
    return path, row


def transfer(request_path, files_path):
    path, row = read_request(request_path)
    prior = json.loads(resolve(row['previous_evidence']).read_text())
    compat = json.loads(resolve(row['compatibility']).read_text())
    selected = {str(path.relative_to(BASE)): digest(path)}
    refs = [row['previous_evidence'], row['compatibility'], prior['transition']['publication_evidence']]
    refs.extend(compat['contract_receipts'].values())
    if row['previous_evidence']['path'] == ADOPTION3:
        revision = load('longhorn-im-revision-3-adopt.py')
        _, impact_refs = revision.read_reuse_impact(compat['reuse_impact'])
        refs.extend({'path': name, 'sha256': sha} for name, sha in impact_refs.items())
        repair_path = BASE/revision.RETIREMENT_REPAIR
        if repair_path.exists():
            repair = json.loads(repair_path.read_text())
            refs.append(reference(repair_path)); refs.extend(repair['contract_receipts'].values())
            for edge in repair['changes'].values(): refs.extend(edge['origins'].values())
    refs.extend({'path':name,'sha256':sha} for name,sha in prior['raw_evidence'].items())
    for ref in row['failure_scopes'].values():
        refs.append(ref); evidence = json.loads(resolve(ref).read_text())
        refs.extend({'path':name,'sha256':sha} for name,sha in evidence['raw_evidence'].items())
    for ref in refs:
        resolve(ref); selected[ref['path']] = ref['sha256']
    if any(not re.fullmatch(r'[A-Za-z0-9_./-]+', name) for name in selected):
        raise ValueError('continuation transfer contains an unsupported file name')
    Path(files_path).write_text(''.join(name+'\n' for name in sorted(selected)))
    return selected


def create(request_path):
    """Capture once within the original launcher's already-held lab leases."""
    path, request_row = read_request(request_path); output = path.with_name('runtime.json')
    if output.exists() or output.is_symlink():
        # A failed empty-store retirement keeps its original pre-uninstall
        # capture. Never recapture an absent installation or overwrite receipts.
        install = load('longhorn-v2-install.py'); _, lab = install.fixture()
        preparation = lab.DIRECTORY/'longhorn-v2-upgrade-preparation.json'
        if not preparation.is_file() or preparation.is_symlink():
            raise ValueError('continuation material is immutable; no owned retirement to resume')
        lab.require_lease(); row = material(reference(output))
        digest(preparation); pending = json.loads(preparation.read_text())
        if (row.get('request') != reference(path) or pending.get('stage') not in ('retiring-empty-new', 'old-ready') or
                pending.get('previous_evidence', {}).get('runtime_continuation') != reference(output) or
                pending.get('baseline_before') != row['baseline']):
            raise ValueError('continuation material is immutable; no matching retirement to resume')
        print('LONGHORN_V2_RUNTIME_CONTINUATION_REUSED_ORIGINAL '+str(output), flush=True)
        return output
    install = load('longhorn-v2-install.py'); _, lab = install.fixture(); lab.require_lease()
    prior = json.loads(resolve(request_row['previous_evidence']).read_text())
    baseline = load('longhorn-v2-upgrade-baseline.py').capture(lab)
    projection = load('longhorn-v2-im-adopt.py').baseline(lab, {
        'baseline_identity': prior['baseline_identity'],
        'native_block_receipt_sha256': prior['independent_readback']['baseline']['native_block_receipt_sha256']})
    row = {**request_row, 'scope': 'focused-runtime-continuation', 'fresh_installation': False,
        'baseline': baseline, 'adoption_projection': projection, 'request': reference(path)}
    save_private(output, row)
    old = os.environ.get(ENV); os.environ[ENV] = str(output)
    try:
        preflight(lab)
    except BaseException:
        output.unlink(); raise
    finally:
        if old is None: os.environ.pop(ENV, None)
        else: os.environ[ENV] = old
    print('LONGHORN_V2_RUNTIME_CONTINUATION_CREATED '+str(output), flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=BASE)
    sub = parser.add_subparsers(dest='mode', required=True)
    prepare = sub.add_parser('request'); prepare.add_argument('--compatibility', required=True)
    prepare.add_argument('--evidence', action='append', required=True)
    sending = sub.add_parser('transfer'); sending.add_argument('--request', required=True); sending.add_argument('--files', required=True)
    live = sub.add_parser('create'); live.add_argument('--request', required=True)
    args = parser.parse_args(); globals()['BASE'] = args.root.resolve()
    if args.mode == 'request': request(args.compatibility, args.evidence)
    elif args.mode == 'transfer': transfer(args.request, args.files)
    else: create(args.request)


if __name__ == '__main__': main()
