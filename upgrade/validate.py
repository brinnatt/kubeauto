#!/usr/bin/env python3
"""Read-only validation and evidence status for the Kubernetes upgrade matrix."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
METHODS = {'analysis', 'static', 'reuse', 'live'}
# These are new migration/recovery/target-installation claims. A static report
# or historical GA receipt cannot establish that the transition actually ran.
LIVE_CASES = {'UPG-05', 'UPG-06', 'UPG-07', 'UPG-12', 'UPG-13', 'UPG-15'}
SHA256 = re.compile(r'[a-f0-9]{64}')


class Invalid(ValueError):
    """An input cannot support the requested acceptance claim."""


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise Invalid(f'duplicate YAML key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def read_mapping(path: Path) -> dict:
    if path.suffix == '.json':
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise Invalid(f'duplicate JSON key: {key}')
                result[key] = value
            return result
        value = json.loads(path.read_text(), object_pairs_hook=pairs)
    else:
        value = yaml.load(path.read_text(), Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise Invalid(f'mapping required: {path.name}')
    return value


def owned_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise Invalid('upgrade-relative repository path required')
    path = (root / name).resolve()
    if not path.is_relative_to((root / 'upgrade').resolve()):
        raise Invalid(f'evidence must remain under upgrade/: {name}')
    return path


def check_hash(root: Path, name: str, expected: str, *, evidence=False) -> None:
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise Invalid(f'invalid SHA256: {name}')
    if evidence:
        path = owned_path(root, name)
    else:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or Path(name).is_absolute():
            raise Invalid(f'input outside product checkout: {name}')
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise Invalid(f'missing or stale input: {name}')


def evidence_valid(root: Path, meta: dict, case: dict) -> None:
    path = owned_path(root, case['evidence'])
    data = read_mapping(path)
    if (data.get('schema_version') != 1 or data.get('accepted') is not True or
            data.get('source') != meta['source'] or data.get('target') != meta['target'] or
            not isinstance(data.get('case_ids'), list) or case['id'] not in data['case_ids']):
        raise Invalid('evidence identity or accepted scope differs from matrix')
    if data.get('method') not in METHODS or not isinstance(data.get('claim'), str) or not data['claim'].strip():
        raise Invalid('evidence method and explicit claim required')
    if case['id'] in LIVE_CASES and data['method'] != 'live':
        raise Invalid('new migration claim requires live evidence')
    if case.get('subcase_expansion_complete') is not True:
        raise Invalid('claim/subcase expansion must be complete before acceptance')
    expected_subcases = case.get('subcases')
    if (not isinstance(expected_subcases, list) or not expected_subcases or
            not all(isinstance(s, str) and s for s in expected_subcases) or
            set(data.get('covered_subcases', [])) != set(expected_subcases)):
        raise Invalid('evidence must cover the exact declared subclaims')
    inputs, refs = data.get('inputs'), data.get('references')
    if not isinstance(inputs, dict) or not inputs or not isinstance(refs, list) or not refs:
        raise Invalid('scoped inputs and local evidence references required')
    for name, digest in inputs.items():
        if name == str(path.relative_to(root)) or name.endswith('/test-matrix.yaml'):
            raise Invalid('evidence cannot depend on itself or mutable acceptance status')
        check_hash(root, name, digest)
    for ref in refs:
        if not isinstance(ref, dict):
            raise Invalid('evidence reference must be a path/SHA256 mapping')
        check_hash(root, ref['path'], ref['sha256'], evidence=True)
    if data['method'] in ('analysis', 'reuse'):
        applicability = data.get('applicability')
        if not isinstance(applicability, dict) or any(
                not isinstance(applicability.get(k), str) or not applicability[k].strip()
                for k in ('conditions', 'differences', 'reason')):
            raise Invalid('analysis/reuse requires conditions, differences and applicability reason')
    if data['method'] == 'static':
        checks = data.get('checks')
        if not isinstance(checks, list) or not checks or any(
                not isinstance(c, dict) or c.get('rc') != 0 or not c.get('command') for c in checks):
            raise Invalid('static evidence requires actual successful checks')
    if data['method'] == 'live':
        if (not data.get('run_id') or data.get('durable_rc') != 0 or
                data.get('finalized_rc') != 0 or data.get('failure_markers') != 0 or
                not case.get('success_marker') or data.get('success_marker') != case['success_marker'] or
                not data.get('cleanup_marker') or not data.get('business_assertions')):
            raise Invalid('live evidence requires durable success, business and cleanup assertions')
        if case['id'] == 'UPG-15' and data['cleanup_marker'] != 'LAB_CLEAN_VERIFY_PASS':
            raise Invalid('final run requires LAB_CLEAN_VERIFY_PASS')
        for key in ('log', 'durable_exit', 'durable_finalized'):
            ref = data.get(key)
            if not isinstance(ref, dict):
                raise Invalid(f'live evidence requires original {key} reference')
            check_hash(root, ref['path'], ref['sha256'], evidence=True)
        for key in ('durable_exit', 'durable_finalized'):
            if owned_path(root, data[key]['path']).read_text().strip() != '0':
                raise Invalid(f'{key} does not record rc=0')
        log = owned_path(root, data['log']['path']).read_text()
        if (data['success_marker'] not in log or data['cleanup_marker'] not in log or
                re.search(r'\[FAIL\]|^ERROR:|Traceback \(most recent call last\)', log, re.MULTILINE)):
            raise Invalid('original log lacks success/cleanup or contains a failure')


def validate(root: Path, path: Path) -> tuple[dict, list[str]]:
    data = read_mapping(path)
    errors = []
    if data.get('schema_version') != 1:
        raise Invalid('unsupported matrix schema_version')
    meta, groups, cases = data.get('meta', {}), data.get('groups', []), data.get('cases', [])
    if (not isinstance(meta, dict) or not isinstance(groups, list) or not isinstance(cases, list) or
            not cases or not groups):
        raise Invalid('matrix requires meta, groups and non-empty cases')
    for key in ('source', 'target'):
        if not re.fullmatch(r'v1\.\d+\.\d+', str(meta.get(key, ''))):
            raise Invalid(f'invalid Kubernetes {key}')
    source = tuple(map(int, meta['source'][1:].split('.')))
    target = tuple(map(int, meta['target'][1:].split('.')))
    if target[1] != source[1] + 1:
        raise Invalid('matrix must describe exactly one minor transition')
    if any(not isinstance(g, dict) or not g.get('id') or not g.get('name') for g in groups):
        raise Invalid('groups require id and name')
    group_ids = {g['id'] for g in groups}
    if len(group_ids) != len(groups):
        raise Invalid('duplicate group id')
    if group_ids != {f'U{i}' for i in range(8)}:
        errors.append('matrix must retain the eight declared capability groups')
    rows = {}
    for case in cases:
        if not isinstance(case, dict):
            raise Invalid('case must be a mapping')
        cid = case.get('id')
        if not isinstance(cid, str) or not re.fullmatch(r'UPG-\d{2}', cid) or cid in rows:
            raise Invalid(f'invalid/duplicate case id: {cid}')
        rows[cid] = case
        if case.get('group') not in group_ids or not case.get('desc'):
            errors.append(f'{cid}: missing description or unknown group')
        if case.get('status') not in ('pending', 'pass', 'fail'):
            errors.append(f'{cid}: invalid status')
        if not isinstance(case.get('depends_on'), list):
            errors.append(f'{cid}: depends_on must be a list')
        if case.get('status') == 'pass':
            try:
                evidence_valid(root, meta, case)
            except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                errors.append(f'{cid}: {exc}')
        elif case.get('status') == 'fail' and not case.get('evidence'):
            errors.append(f'{cid}: failed case must preserve its failure evidence')
    if set(rows) != {f'UPG-{i:02}' for i in range(1, 16)}:
        errors.append('matrix must retain all fifteen declared acceptance cases')
    def visit(cid, trail=()):
        if cid not in rows:
            raise Invalid(f'unknown dependency: {cid}')
        if cid in trail:
            raise Invalid(f'dependency cycle: {cid}')
        for dep in rows[cid].get('depends_on') or []:
            visit(dep, trail + (cid,))
            if rows[cid]['status'] == 'pass' and rows[dep]['status'] != 'pass':
                raise Invalid(f'{cid}: prerequisite {dep} is not pass')
    try:
        for cid in rows:
            visit(cid)
        if set(rows.get('UPG-15', {}).get('depends_on', [])) != set(rows) - {'UPG-15'}:
            raise Invalid('UPG-15 must depend on all other acceptance cases')
    except Invalid as exc:
        errors.append(str(exc))
    return data, errors


def snapshot(data: dict) -> dict:
    cases = data['cases']
    passed = sum(c['status'] == 'pass' for c in cases)
    return {'pass': passed, 'total': len(cases), 'pending_or_fail': len(cases) - passed,
            'fail': sum(c['status'] == 'fail' for c in cases),
            'completed_groups': sum(all(c['status'] == 'pass' for c in cases if c['group'] == g['id'])
                                    and any(c['group'] == g['id'] for c in cases) for g in data['groups']),
            'total_groups': len(data['groups']), 'groups': data['groups'],
            'cases': [{k: c[k] for k in ('id', 'group', 'desc', 'status')} for c in cases]}


def immutable_pins(value) -> bool:
    """Version groups and per-image/chart maps use the same immutable leaves."""
    if isinstance(value, dict):
        return bool(value) and all(isinstance(key, str) and key.strip() and immutable_pins(pin)
                                   for key, pin in value.items())
    return (isinstance(value, str) and bool(value.strip()) and
            not re.search(r'(^|[:/])(latest|main|master|stable|nightly|HEAD)([.@-]|$)', value, re.I) and
            not re.search(r'[-._](alpha|beta|rc|dev|snapshot)([.\d-]|$)', value, re.I))


def official_copies(source: dict) -> list[dict]:
    """Require every captured page of a single- or multi-page observation."""
    contents = []
    if 'obtained_content' in source:
        contents.append(source['obtained_content'])
    if 'obtained_contents' in source:
        pages = source['obtained_contents']
        if not isinstance(pages, list) or not pages:
            raise Invalid('obtained_contents must be a nonempty list')
        contents.extend(pages)
    if not contents or any(not isinstance(page, dict) or
                           not isinstance(page.get('local_copy'), dict) for page in contents):
        raise Invalid('official source not obtained')
    return [page['local_copy'] for page in contents]


def combination_errors(root: Path, directory: Path, meta: dict) -> list[str]:
    """Check the declared freeze against its inputs; never infer compatibility."""
    release = read_mapping(directory / 'release.yaml')
    catalog = read_mapping(root / 'upgrade/catalog.yaml')
    official = read_mapping(root / 'upgrade/official-sources.yaml')
    errors = []
    if (release.get('source_kubernetes'), release.get('target_kubernetes')) != (
            meta['source'], meta['target']):
        errors.append('release endpoints differ from matrix')
    if release.get('review', {}).get('release_frozen') is not True:
        errors.append('release combination is not frozen')
    expected = {c['id'] for c in catalog['components']}
    components = release.get('component_decisions', [])
    ids = [c['component_id'] for c in components]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        errors.append('component decisions must cover the exact catalog without duplicates')
    sources = {s['id']: s for s in official['sources']}
    if len(sources) != len(official['sources']):
        errors.append('duplicate official source id')
    edge_ids = set()
    for component in components:
        cid = component['component_id']
        prefix = f'{cid}: '
        decision = component.get('decision')
        if decision not in {'keep', 'update', 'bridge', 'replace', 'not-applicable'}:
            errors.append(prefix + 'unresolved component decision')
        if not component.get('rationale'):
            errors.append(prefix + 'decision rationale required')
        pins = component.get('target_pins')
        if decision != 'not-applicable' and (
                not isinstance(pins, dict) or not immutable_pins(pins)):
            errors.append(prefix + 'explicit immutable target pins required')
        refs = component.get('source_ids')
        if not isinstance(refs, list) or not refs:
            errors.append(prefix + 'official applicability sources required')
            refs = []
        for sid in refs:
            source = sources.get(sid, {})
            if not source.get('facts'):
                errors.append(prefix + f'official source not obtained: {sid}')
                continue
            try:
                for copied in official_copies(source):
                    check_hash(root, copied['path'], copied['sha256'], evidence=True)
                    if owned_path(root, copied['path']).stat().st_size != copied['bytes']:
                        raise Invalid('snapshot size differs')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(prefix + f'{sid}: {exc}')
        edges = component.get('transition_edges')
        if decision != 'not-applicable' and (not isinstance(edges, list) or not edges):
            errors.append(prefix + 'platform transition or component migration edge required')
        if decision == 'bridge' and not any(edge.get('bridge') for edge in edges or []):
            errors.append(prefix + 'explicit bridge required')
        for edge in edges or []:
            eid = edge.get('edge_id')
            if not eid or eid in edge_ids:
                errors.append(prefix + 'missing or duplicate migration edge id')
            edge_ids.add(eid)
            for key in ('source', 'target', 'product_entry', 'irreversible_point', 'recovery', 'cleanup'):
                if not isinstance(edge.get(key), str) or not edge[key].strip():
                    errors.append(prefix + f'{eid}: {key} required')
            for key in ('preconditions', 'steps', 'success_conditions', 'official_source_ids'):
                value = edge.get(key)
                if not isinstance(value, list) or not value or any(
                        not isinstance(v, str) or not v.strip() for v in value):
                    errors.append(prefix + f'{eid}: {key} required')
            if not set(edge.get('official_source_ids') or []) <= set(refs):
                errors.append(prefix + f'{eid}: migration sources outside component references')
            deps = edge.get('depends_on')
            if not isinstance(deps, list) or not set(deps) <= {f'UPG-{i:02}' for i in range(1, 16)}:
                errors.append(prefix + f'{eid}: invalid acceptance dependencies')
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matrix', type=Path)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--require-pass', action='store_true')
    parser.add_argument('--final-prerequisites', action='store_true')
    parser.add_argument('--combination-preflight', action='store_true',
                        help='read-only freeze completeness check, not compatibility certification')
    args = parser.parse_args(argv)
    try:
        data, errors = validate(ROOT, args.matrix.resolve())
        if args.require_pass or args.final_prerequisites:
            errors += [f"{c['id']}: prerequisite is {c['status']}" for c in data['cases']
                       if c['status'] != 'pass' and not (args.final_prerequisites and c['id'] == 'UPG-15')]
        if args.combination_preflight or args.final_prerequisites:
            errors += combination_errors(ROOT, args.matrix.resolve().parent, data['meta'])
        if args.final_prerequisites:
            meta = data['meta']
            if meta.get('execution_enabled') is not True or not meta.get('runner'):
                errors.append('execution remains disabled or runner missing')
        if errors:
            print('UPGRADE_MATRIX_VALIDATION_FAIL\n' + '\n'.join(errors))
            return 1
        if args.status:
            print(json.dumps(snapshot(data), ensure_ascii=False, indent=2))
        else:
            print('UPGRADE_MATRIX_VALIDATION_PASS')
        return 0
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        print(f'UPGRADE_MATRIX_VALIDATION_FAIL\n{exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
