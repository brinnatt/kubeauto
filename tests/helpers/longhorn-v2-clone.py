#!/usr/bin/env python3
"""Compose LONGHORN-49 from explicit completed linked and upgrade executions.

This is an evidence consumer. It never launches a clone or repeats an accepted
full-copy scope; the original linked aggregate owns all live mutations.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

BASE = Path('/usr/local/kubeauto')
IMPACT = 'tests/evidence/longhorn-v2-full-copy-reuse-impact-20261021.json'
SCOPES = ('full-copy-clone', 'full-copy-lifecycle')
STAGES = ('linked-clone', 'linked-lifecycle', 'incomplete-clone')
INPUTS = ('roles/longhorn/files/longhorn.py', 'roles/longhorn/files/block-check.py',
    'roles/longhorn/files/images.json', 'tests/helpers/longhorn-lab.py',
    'tests/helpers/longhorn-v2-lab.py', 'tests/helpers/longhorn-v2-core.py',
    'tests/helpers/longhorn-v2-install.py', 'tests/helpers/longhorn-v2-im-adopt.py',
    'tests/helpers/longhorn-v2-clones.py', 'tests/helpers/longhorn-v2-clone-lifecycle.py',
    'tests/helpers/longhorn-v2-linked-clones.py', 'tests/helpers/longhorn-v2-linked-clones.sh',
    'tests/helpers/longhorn-v2-linked-lifecycle.py', 'tests/helpers/longhorn-v2-linked-lifecycle.sh',
    'tests/helpers/longhorn-v2-incomplete-clone.py', 'tests/helpers/longhorn-v2-incomplete-clone.sh',
    'tests/helpers/longhorn-v2-linked-acceptance.sh', 'tests/helpers/longhorn-v2-clone.py',
    'tests/helpers/longhorn-v2-upgrade-baseline.py', 'tests/helpers/longhorn-parallel.py',
    'tests/helpers/run-durable-gate.sh', 'tests/helpers/longhorn-v2-runtime-continuation.py',
    'tests/helpers/longhorn-im-revision-3-adopt.py', 'tests/fixtures/longhorn-im-revision-3-plan.json')
FAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)')


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('clone evidence requires a regular non-symlink file: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def referenced(base, name):
    name = Path(name)
    if name.is_absolute() or '..' in name.parts:
        raise ValueError('clone evidence reference escapes the product directory')
    path = base/name
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('clone evidence symlink escapes the product directory')
    digest(path)
    return path


def reference(base, path):
    return {'path': str(path.relative_to(base)), 'sha256': digest(path)}


def load(base, name):
    spec = importlib.util.spec_from_file_location('clone49_'+name.replace('-', '_'), base/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if hasattr(module, 'BASE'): module.BASE = base
    return module


def markers(lines, required):
    if any(FAILURE.match(line) for line in lines):
        raise ValueError('clone owning execution contains a failure marker')
    for item in required:
        if lines.count(item) != 1:
            raise ValueError('clone owning execution lacks unique marker: '+item)


def observation(lines, prefix):
    values = [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
    if len(values) != 1:
        raise ValueError('clone execution lacks unique observation: '+prefix)
    return values[0]


def clone_owning_ast_sha256(node, key):
    """Preserve original proofs across two exact reviewed integration changes.

    Qualified identities were populated; normalize their accepted nullable
    read. The retired-fault cleanup branch is unreachable from clone owners.
    Every other statement and every clone assertion remains compared unchanged.
    """
    canonical = ast.parse(ast.unparse(node)).body[0]
    if key == 'roles/longhorn/files/longhorn.py:check_block_identity':
        # Previously qualified identities were populated. Reconstruct only the
        # accepted nullable read; compare every other statement to the old AST.
        current = ast.parse("status = (current.get('status', {}).get('diskStatus') or {}).get(name) or {}").body[0]
        previous = ast.parse("status = current.get('status', {}).get('diskStatus', {}).get(name, {})").body[0]
        for statement in ast.walk(canonical):
            if ast.dump(statement, include_attributes=False) == ast.dump(current, include_attributes=False):
                statement.value = previous.value
    if key == 'tests/helpers/longhorn-lab.py:cleanup_business':
        guard = ast.parse("if journal.get('v2_all_replicas_fault'):\n"
            "    cleanup_retired_v2_endpoint(journal['volume'], journal['volume_uid'], 2*1024**3)\n").body[0]
        if (len(canonical.body) >= 3 and
                ast.dump(canonical.body[-3], include_attributes=False) == ast.dump(guard, include_attributes=False) and
                ast.dump(canonical.body[-2], include_attributes=False) ==
                ast.dump(ast.parse('record.unlink()').body[0], include_attributes=False)):
            del canonical.body[-3]
    return hashlib.sha256(ast.dump(canonical, include_attributes=False).encode()).hexdigest()


def preflight_full_copy(base):
    """Verify the reviewed finite reuse proof and its original raw evidence."""
    base = Path(base); path = referenced(base, IMPACT); impact = json.loads(path.read_text())
    transport_refs = {IMPACT: digest(path)}
    if impact.get('matrix_item') != 'LONGHORN-49' or len(impact.get('historical_scopes', [])) != 2:
        raise ValueError('clone full-copy reuse proof has incomplete scope')
    accepted = []
    for scope, bound in zip(SCOPES, impact['historical_scopes']):
        expected = 'tests/evidence/longhorn-v2-'+scope+'-qualification-'
        name = bound['evidence']
        if not name.startswith(expected) or bound.get('status') != 'historical-scope-verified':
            raise ValueError('clone full-copy reuse scope differs')
        evidence_path = referenced(base, name)
        evidence_sha = digest(evidence_path)
        if evidence_sha != bound['sha256']:
            raise ValueError('clone accepted full-copy evidence changed')
        row = json.loads(evidence_path.read_text())
        if (row.get('status') != 'pass' or row.get('rc') != 0 or row.get('failure_markers') != 0 or
                row.get('remote_input_verification', {}).get('mismatches') != 0 or not row.get('raw_logs')):
            raise ValueError('clone full-copy evidence is not accepted')
        for name, expected_sha in row['raw_logs'].items():
            actual_sha = digest(referenced(base, name))
            if actual_sha != expected_sha:
                raise ValueError('clone historical raw digest changed')
            transport_refs[name] = actual_sha
        logs = [referenced(base, name) for name in row['raw_logs']
                if name.endswith('/gate.log') and '/frozen-source/' not in name]
        states = [referenced(base, name) for name in row['raw_logs'] if name.endswith('/durable-status.txt')]
        prefix = 'LONGHORN_V2_'+scope.upper().replace('-', '_')+'_QUALIFICATION'
        if len(logs) != 1 or len(states) != 1:
            raise ValueError('clone historical owning execution is ambiguous')
        markers(logs[0].read_text().splitlines(), (prefix+'_SCOPED_CLEAN_PASS', prefix+'_PASS', 'LONGHORN_GATE_EXIT rc=0'))
        state = states[0].read_text()
        if (state != row.get('durable_status') or
                not all(re.search(r'(?:^| )'+item+r'(?: |$)', state)
                        for item in ('state=exited', 'rc=0', 'failure_markers=0'))):
            raise ValueError('clone historical durable execution is incomplete')
        for key, expected_sha in bound['unchanged_owning_functions_ast_sha256'].items():
            name, function = key.rsplit(':', 1)
            tree = ast.parse(referenced(base, name).read_text())
            nodes = [node for node in tree.body if getattr(node, 'name', None) == function]
            if (len(nodes) != 1 or clone_owning_ast_sha256(nodes[0], key) != expected_sha):
                raise ValueError('clone full-copy owning mechanism changed: '+key)
        ref = {'path': str(evidence_path.relative_to(base)), 'sha256': evidence_sha}
        transport_refs[ref['path']] = evidence_sha
        accepted.append(ref)
    for dependency in ('current_adoption_dependency', 'artifact_publication_evidence'):
        ref = impact[dependency]
        actual_sha = digest(referenced(base, ref['path']))
        if actual_sha != ref['sha256']:
            raise ValueError('clone reviewed artifact/adoption reuse dependency changed')
        transport_refs[ref['path']] = actual_sha
    revision3 = None
    catalog = json.loads(referenced(base, 'roles/longhorn/files/images.json').read_text())
    if catalog['1.12.1']['longhorn-instance-manager'] == 'v1.12.1-kubeauto.3':
        revision = load(base, 'longhorn-im-revision-3-adopt.py')
        impact3, refs = revision.read_reuse_impact()
        if impact3['full_copy_impact'] != {'path': IMPACT, 'sha256': transport_refs[IMPACT]}:
            raise ValueError('clone revision3 impact changed the original historical full-copy boundary')
        revision3 = {'path': revision.REUSE_IMPACT, 'sha256': refs[revision.REUSE_IMPACT]}
        transport_refs.update(refs)
    return {'revision3_reuse': revision3, 'full_copy': accepted, 'reuse_impact': {'path': IMPACT, 'sha256': transport_refs[IMPACT]},
            'transport_refs': transport_refs,
            'inputs': {name: digest(referenced(base, name)) for name in INPUTS}}


def focused_execution(base, directory):
    """Compose independently complete child scopes; preserve a failed parent.

    Only the original child logs and their durable receipts sign business.
    The failed parent supplies its original before capture, never a PASS.
    """
    path = directory/'composition.json'
    digest(path); row = json.loads(path.read_text())
    if (row.get('schema') != 1 or row.get('status') != 'original-stage-composition' or
            row.get('matrix_item') != 'LONGHORN-49' or set(row.get('stages', {})) != set(STAGES)):
        raise ValueError('clone composition requires all three original scopes')
    refs = {}
    def bound(ref):
        path = referenced(base, ref['path'])
        if digest(path) != ref['sha256']: raise ValueError('clone original scope bytes changed')
        refs[ref['path']] = ref
        return path
    before = observation(bound(row['original_parent_log']).read_text().splitlines(), 'LONGHORN_V2_CLONE_BASELINE_BEFORE ')
    if not before or set(before) != {'baseline', 'inputs'}:
        raise ValueError('clone original before capture is incomplete')
    original = bound(row['original_consumer'])
    if digest(original) != before['inputs'].get('tests/helpers/longhorn-v2-clone.py'):
        raise ValueError('clone original creation consumer differs')
    def creation_contract(source, current=False):
        tree = ast.parse(source)
        if current:
            extra = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'focused_execution']
            if (len(extra) != 1 or extra[0].decorator_list or extra[0].args.defaults or extra[0].args.kw_defaults):
                raise ValueError('clone composition definition has import side effects')
            tree.body.remove(extra[0])
        # Snapshot/receipt creation calls neither the aggregate reader nor the
        # full-copy AST reader. Keep their definitions/signatures comparable;
        # preflight_full_copy separately executes the current owning reader.
        for name in ('linked_execution', 'clone_owning_ast_sha256'):
            nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
            if len(nodes) != 1: raise ValueError('clone original aggregate consumer changed')
            nodes[0].body = [ast.Pass()]
        return ast.dump(tree, include_attributes=False)
    if creation_contract(original.read_text()) != creation_contract((base/'tests/helpers/longhorn-v2-clone.py').read_text(), True):
        raise ValueError('clone creation snapshot/receipt consumer changed')
    identity = None
    for stage in STAGES:
        scope = row['stages'][stage]
        pins = json.loads(bound(scope['inputs']).read_text())
        # Each original fixed shell executes only its own live driver. The
        # evidence reader's unchanged snapshot/receipt path is proven above;
        # later, uncalled drivers do not invalidate completed business.
        unused = {'tests/helpers/longhorn-v2-clone.py'}
        if stage in ('linked-clone', 'linked-lifecycle'):
            unused.add('tests/helpers/longhorn-v2-incomplete-clone.py')
        if stage == 'linked-clone':
            unused.add('tests/helpers/longhorn-v2-linked-lifecycle.py')
        for name in set(INPUTS)-unused:
            if pins.get(name) != digest(base/name):
                # Creation predates the reviewed adoption-reader repair. Use
                # its existing finite contract, including original bytes and
                # durable tests; keep every product/live driver strict.
                if (stage == 'linked-clone' and name == 'tests/helpers/longhorn-im-revision-3-adopt.py' and
                        load(base, 'longhorn-im-revision-3-adopt.py').gate_input_current(name, pins.get(name))):
                    continue
                raise ValueError('LONGHORN-49 focused input changed: '+name)
        for suffix in ('exit', 'finalized'):
            if bound(scope[suffix]).read_text().strip() != '0':
                raise ValueError('LONGHORN-49 focused original durable state is nonzero')
        owner = json.loads(bound(scope['owner']).read_text())
        current = {key: owner[key] for key in before['baseline']['baseline_identity']}
        if not all(current.values()) or (identity is not None and identity != current):
            raise ValueError('LONGHORN-49 focused stages have different source identities')
        if current != before['baseline']['baseline_identity']:
            raise ValueError('clone focused owner differs from original before capture')
        identity = current
        lines = bound(scope['log']).read_text().splitlines()
        prefix = 'LONGHORN_V2_'+stage.upper().replace('-', '_')+'_QUALIFICATION'
        terminal = prefix+'_EXIT rc=0' if stage == 'linked-clone' else 'LONGHORN_GATE_EXIT rc=0'
        markers(lines, (prefix+'_SCOPED_CLEAN_PASS', prefix+'_PASS', terminal))
        required = {
            'linked-clone': ('LONGHORN_V2_LINKED_NESTED_BUSINESS_PASS', 'LONGHORN_V2_LINKED_FIXTURES_CLEAN_PASS'),
            'linked-lifecycle': ('LONGHORN_V2_LINKED_LIFECYCLE_PASS', 'LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS'),
            'incomplete-clone': ('LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS', 'LONGHORN_V2_INCOMPLETE_CLONE_FIXTURES_CLEAN_PASS'),
        }
        markers(lines, required[stage])
        prefixes = {
            'linked-clone': ('LONGHORN_V2_LINKED_NESTED_RESULT ',),
            'linked-lifecycle': ('LONGHORN_V2_LINKED_DEPENDENCY_REBUILD_VERIFIED ', 'LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED '),
            'incomplete-clone': ('LONGHORN_V2_INCOMPLETE_CLONE_RESULT ',),
        }
        for prefix in prefixes[stage]:
            if not observation(lines, prefix):
                raise ValueError('LONGHORN-49 focused business result is empty')
    return before['baseline'], {'receipts': {'composition': reference(base, path), **refs},
        'stages': [{'stage': stage, 'durable': {'exit': 0, 'finalized': 0}} for stage in STAGES],
        'boundary': 'Original complete child scopes only; failed parent remains failed'}


def linked_execution(base, directory):
    """Consume an explicit original aggregate, including its nested rc fences."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('clone requires an explicit regular fixed-runner directory')
    relative = directory.resolve().relative_to(base.resolve()).as_posix()
    if directory.resolve().is_relative_to((base/'logs/longhorn-cases').resolve()) and (directory/'composition.json').exists():
        return focused_execution(base, directory)
    full = re.fullmatch(r'logs/longhorn-full/run\.[A-Za-z0-9-]+/raw/'
        r'v1-ownership-v2-linked-acceptance/kubeauto-longhorn-parallel\.[A-Za-z0-9]+', relative)
    if full:
        # The full owner archives these original bytes and their SHA256 after
        # accepting the owning parallel stage. Never synthesize a gate/status
        # layout for this real nested execution.
        log_name = 'v2.log'; durable_names = ('v2.exit', 'v2.finalized')
        names = (log_name, *durable_names)
    elif directory.resolve().is_relative_to((base/'logs/longhorn-cases').resolve()):
        log_name = 'gate.log'; durable_names = ('durable-exit', 'durable-finalized')
        names = (log_name, *durable_names, 'durable-status.txt')
        digest(directory/'durable-status.txt'); state = (directory/'durable-status.txt').read_text()
        if not all(re.search(r'(?:^| )'+item+r'(?: |$)', state)
                   for item in ('state=exited', 'rc=0', 'failure_markers=0')):
            raise ValueError('clone aggregate durable status is incomplete')
    else:
        raise ValueError('clone requires an explicit owning fixed-runner or full archived directory')
    for name in durable_names:
        digest(directory/name)
        if (directory/name).read_text().strip() != '0':
            raise ValueError('clone aggregate durable execution did not finalize rc=0')
    digest(directory/log_name); lines = (directory/log_name).read_text().splitlines()
    if full: markers(lines, ('LONGHORN_PARALLEL_V2_EXIT rc=0',))
    markers(lines, ('LONGHORN_V2_LINKED_ACCEPTANCE_SCOPED_CLEAN_PASS', 'LONGHORN_V2_LINKED_ACCEPTANCE_PASS'))
    before = observation(lines, 'LONGHORN_V2_CLONE_BASELINE_BEFORE ')
    nested = []; ordering = [next(i for i, line in enumerate(lines) if line.startswith('LONGHORN_V2_CLONE_BASELINE_BEFORE '))]
    for stage in STAGES:
        prefix = 'LONGHORN_V2_'+stage.upper().replace('-', '_')+'_QUALIFICATION'
        markers(lines, (prefix+'_SCOPED_CLEAN_PASS', prefix+'_PASS', prefix+'_EXIT rc=0',
            'LONGHORN_V2_LINKED_ACCEPTANCE_STAGE_PASS stage='+stage+' durable_rc=0 finalized=0'))
        proof = observation(lines, 'LONGHORN_V2_CLONE_STAGE_RECEIPT stage='+stage+' ')
        if proof != {'exit': 0, 'finalized': 0}:
            raise ValueError('clone nested stage has nonzero original durable receipt')
        ordering.extend((lines.index(prefix+'_EXIT rc=0'),
                         lines.index('LONGHORN_V2_CLONE_STAGE_RECEIPT stage='+stage+' '+json.dumps(proof, sort_keys=True)),
                         lines.index('LONGHORN_V2_LINKED_ACCEPTANCE_STAGE_PASS stage='+stage+' durable_rc=0 finalized=0')))
        nested.append({'stage': stage, 'durable': proof})
    markers(lines, ('LONGHORN_V2_LINKED_NESTED_BUSINESS_PASS', 'LONGHORN_V2_LINKED_LIFECYCLE_PASS',
        'LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS', 'LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS'))
    for prefix in ('LONGHORN_V2_LINKED_NESTED_RESULT ', 'LONGHORN_V2_LINKED_DEPENDENCY_REBUILD_VERIFIED ',
                   'LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED ', 'LONGHORN_V2_INCOMPLETE_CLONE_RESULT '):
        if not observation(lines, prefix):
            raise ValueError('clone linked business result is empty')
    after = observation(lines, 'LONGHORN_V2_CLONE_BASELINE_AFTER ')
    ordering.append(lines.index(next(line for line in lines if line.startswith('LONGHORN_V2_CLONE_BASELINE_AFTER '))))
    ordering.append(lines.index('LONGHORN_V2_LINKED_ACCEPTANCE_SCOPED_CLEAN_PASS'))
    if ordering != sorted(set(ordering)):
        raise ValueError('clone capture, original durable fences and stage completion are out of order')
    if not before or before != after or set(before) != {'baseline', 'inputs'}:
        raise ValueError('clone original before/after independent capture differs')
    if before['inputs'] != {name: digest(referenced(base, name)) for name in INPUTS}:
        # A full continuation already binds its immutable owner receipt and
        # reviewed current inputs. Consume that same proof; do not rewrite the
        # original capture or rerun successful linked business for a reader fix.
        allowed = {'roles/longhorn/files/images.json',
                   'tests/helpers/longhorn-v2-upgrade-baseline.py',
                   'tests/helpers/longhorn-im-revision-3-adopt.py',
                   'tests/helpers/longhorn-v2-clone.py'}
        changed = {name for name in set(before['inputs']) | set(INPUTS)
                   if before['inputs'].get(name) != digest(referenced(base, name))}
        if not full or not changed <= allowed or set(before['inputs']) != set(INPUTS):
            raise ValueError('clone owning frozen inputs differ from the current consumer')
        reader = load(base, 'longhorn-full-evidence.py')
        work = reader.work_directory()
        if directory.parent.parent.parent != work:
            raise ValueError('clone continuation belongs to another full run')
        current = reader.check_inputs(work)
        _, owner, _ = reader.accepted_stage('v1-ownership-v2-linked-acceptance')
        if before['inputs'] != {name: owner['frozen_inputs'].get(name) for name in INPUTS}:
            raise ValueError('clone capture differs from its accepted owning inputs')
        copies = {item['copy']['path']: item['copy']['sha256']
                  for item in owner['nested_receipts']}
        for name in names:
            ref = reference(base, directory/name)
            if copies.get(ref['path']) != ref['sha256']:
                raise ValueError('clone continuation changed an original owning receipt')
        if any(current.get(name) != digest(referenced(base, name)) for name in INPUTS):
            raise ValueError('clone continuation current inputs changed during readback')
    return before['baseline'], {'receipts': {name: reference(base, directory/name) for name in names},
        'stages': nested}


def validate_clone49(base, directory):
    """Unique 49 composition interface for canonical and full-delivery consumers."""
    base = Path(base); reused = preflight_full_copy(base)
    baseline, linked = linked_execution(base, Path(directory))
    upgrade = load(base, 'longhorn-v2-upgrade-baseline.py')
    evidence, current, _ = upgrade.accepted_evidence()
    upgrade_path = (upgrade.evidence_path(upgrade.EVIDENCE) if hasattr(upgrade, 'evidence_path')
                    else base/upgrade.EVIDENCE)
    transition = evidence['transition']
    # A real 52 transition preserves original core/hardware but replaces native
    # installation identities. Accept either explicit side; never compare the
    # present namespace with a historical full-copy namespace.
    stable = load(base, 'longhorn-v2-runtime-continuation.py').stable
    side = None
    for candidate in ('after', 'before'):
        try:
            stable(baseline, transition[candidate])
        except ValueError:
            continue
        side = candidate
        break
    if side is None:
        raise ValueError('clone original capture is unrelated to the accepted real 52 transition')
    return {'schema': 1, 'status': 'pass', 'signed_scope': 'v2-clone', 'matrix_item': 'LONGHORN-49',
        'version': '1.12.1', 'rc': 0, 'failure_markers': 0, 'durable': {'exit': 0, 'finalized': 0},
        'full_copy_reuse': reused, 'linked': linked, 'linked_upgrade_side': side,
        'baseline': baseline, 'accepted_upgrade_baseline': current,
        'upgrade_evidence': reference(base, upgrade_path),
        'boundary': '49 evidence composition only; no replay of full-copy or linked scopes, no overall delivery PASS'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'snapshot', 'stage-receipt', 'finish'))
    parser.add_argument('--root', type=Path, default=BASE)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--phase', choices=('before', 'after'))
    parser.add_argument('--stage', choices=STAGES)
    args = parser.parse_args(); base = args.root.resolve()
    if args.mode == 'preflight':
        print(json.dumps(preflight_full_copy(base), sort_keys=True))
    elif args.mode == 'snapshot':
        if not args.phase: parser.error('snapshot requires --phase')
        install = load(base, 'longhorn-v2-install.py'); _, lab = install.fixture(); lab.require_lease()
        baseline = load(base, 'longhorn-v2-upgrade-baseline.py').capture(lab)
        value = {'baseline': baseline, 'inputs': {name: digest(referenced(base, name)) for name in INPUTS}}
        print('LONGHORN_V2_CLONE_BASELINE_'+args.phase.upper()+' '+json.dumps(value, sort_keys=True), flush=True)
    elif args.mode == 'stage-receipt':
        if not args.directory or not args.stage: parser.error('stage-receipt requires --directory and --stage')
        values = {}
        for suffix in ('exit', 'finalized'):
            path = args.directory/(args.stage+'.'+suffix); digest(path)
            if path.read_text().strip() != '0': raise ValueError('linked original durable receipt is nonzero')
            values[suffix] = 0
        print('LONGHORN_V2_CLONE_STAGE_RECEIPT stage='+args.stage+' '+json.dumps(values, sort_keys=True), flush=True)
    else:
        if not args.directory: parser.error('finish requires --directory; no automatic linked replay')
        result = validate_clone49(base, args.directory)
        path = args.directory/'clone-acceptance.json'
        if path.exists() or path.is_symlink():
            digest(path)
            if json.loads(path.read_text()) != result:
                raise ValueError('clone original acceptance composition changed')
        else:
            with path.open('x') as stream:
                stream.write(json.dumps(result, sort_keys=True)+'\n')
            path.chmod(0o600)
        print('LONGHORN_V2_CLONE_SCOPED_CLEAN_PASS')
        print('LONGHORN_V2_CLONE_PASS')


if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        traceback.print_exc(); sys.exit(1)
