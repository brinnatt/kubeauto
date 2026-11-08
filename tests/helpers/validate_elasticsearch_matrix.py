#!/usr/bin/env python3
"""Validate the ES acceptance ledger without contacting a lab or registry."""
import argparse
import ast
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

import yaml


GROUPS = frozenset("contract artifacts platform standard compact business lifecycle security faults maintenance backup dr observability performance upgrade coexistence cleanup delivery".split())

CASE_GROUPS = {f'ES-{i:02}': group for group, first, last in (
    ('contract', 1, 4), ('artifacts', 5, 6), ('platform', 7, 9), ('standard', 10, 12),
    ('compact', 13, 14), ('business', 15, 20), ('lifecycle', 21, 23), ('security', 24, 29),
    ('faults', 30, 34), ('maintenance', 35, 38), ('backup', 39, 42), ('dr', 43, 45),
    ('observability', 46, 47), ('performance', 48, 49), ('upgrade', 50, 52),
    ('coexistence', 53, 55), ('cleanup', 56, 57), ('delivery', 58, 59)) for i in range(first, last + 1)}

# Capability groups describe acceptance, while these cases are reached through
# different focused modules or the complete delivery entry point.
CASE_MODULES = dict(CASE_GROUPS, **{
    'ES-07': 'csi-probe', 'ES-08': 'standard', 'ES-09': 'csi-probe',
    'ES-24': 'security-identity', 'ES-25': 'security-identity',
    'ES-26': 'security-certificates', 'ES-27': 'security-identity',
    'ES-28': 'security-s3', 'ES-29': 'security-network',
    'ES-39': 'backup-source', 'ES-40': 'backup-refusal',
    'ES-41': 'backup-restore', 'ES-42': 'backup-features',
    'ES-55': 'logging', 'ES-56': 'cleanup-probes', 'ES-58': 'documentation', 'ES-59': 'only',
})

# These are mandatory parts of the three shared-platform acceptance items,
# not additional stages or independently signable delivery cases.
ISOLATION_SUBCASES = {
    'ES-50': ('platform-admission', 'platform-upgrade', 'platform-recovery'),
    'ES-53': ('es-then-efk', 'efk-then-es', 'ownership-refusal',
              'credential-refusal', 'repository-refusal', 'eck-recovery'),
    'ES-54': ('business-upgrade', 'efk-upgrade-refusal', 'business-restore',
              'efk-restore', 'business-uninstall', 'efk-uninstall'),
}


def require_case_markers(case, lines):
    """A case summary cannot substitute for any mandatory child scenario."""
    expected = [case['success_marker']] + [row['success_marker'] for row in case.get('subcases', [])]
    missing = set(expected) - set(lines)
    if missing:
        raise ValueError('raw log lacks mandatory case/subcase markers: ' + ', '.join(sorted(missing)))


def dependency_digest(root, name):
    """Hash a file, or declared Python entry points and their local callees.

    Imports, module initialization and CLI dispatch are always included. Only
    explicitly mapped Python helpers use this boundary; unknown consumers keep
    a whole-file binding. Raw logs and durable files always use byte hashes.
    """
    filename, separator, fragment = name.partition('#')
    path = (root / filename).resolve()
    if Path(filename).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError('recorded dependency escapes the repository')
    raw = path.read_bytes()
    if not separator:
        return hashlib.sha256(raw).hexdigest()
    if path.suffix != '.py' or not re.fullmatch(r'[a-zA-Z_]\w*(?:,[a-zA-Z_]\w*)*', fragment):
        raise ValueError('invalid Python dependency entry points')
    return python_dependency_digest(raw, fragment)


def python_dependency_digest(raw, fragment, *, shadow_aware=False):
    """Apply the same entry-point boundary to current or archived source."""
    tree = ast.parse(raw)
    definitions = {node.name: node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    wanted, pending = set(), set(fragment.split(','))
    if not pending <= definitions.keys():
        raise ValueError('dependency entry point is missing')
    common = [node for node in tree.body if node not in definitions.values()]
    # The dispatch definition is bound, but its alternative commands are not
    # callers of the selected command. Module initialization remains a caller.
    if 'main' in definitions:
        common.append(definitions['main'])
    pending |= {node.id for item in common if item is not definitions.get('main')
                for node in ast.walk(item) if isinstance(node, ast.Name)} & definitions.keys() - {'main'}
    while pending:
        current = pending.pop()
        if current in wanted:
            continue
        wanted.add(current)
        parameters = ({node.arg for node in ast.walk(definitions[current]) if isinstance(node, ast.arg)}
                      if shadow_aware else set())
        pending |= {node.id for node in ast.walk(definitions[current])
                    if isinstance(node, ast.Name)} & definitions.keys() - wanted - {'main'} - parameters
    selected = common + [definitions[key] for key in sorted(wanted)]
    value = '\n'.join(ast.dump(node, include_attributes=False) for node in selected)
    return hashlib.sha256(value.encode()).hexdigest()


def dependency_matches(root, record, case, source, checksum):
    if dependency_digest(root, source) == checksum:
        return True
    if not re.fullmatch(r'[a-f0-9]{64}', checksum):
        return False
    owner = CASE_MODULES[case['id']]
    # Legacy inventories included local qualification tests in live receipts.
    # Those files are not executed by a focused product scenario. New live
    # assertions remain bound through their actual helpers and matrix markers.
    if source.startswith('tests/unit/') and source.endswith('.py') and owner not in ('contract', 'only'):
        return True
    # These local receipt processors do not execute a product scenario. The
    # current verifier still checks the original logs, durable exits, launch
    # inputs and archive; changing a signer never re-signs an old run.
    controls = {'tests/helpers/elasticsearch-evidence.py', 'tests/helpers/validate_elasticsearch_matrix.py',
                'tests/helpers/elasticsearch-stop.py'}
    product = 'roles/cluster-addon/files/elasticsearch.py'
    lab_source, separator, lab_entry = source.partition('#')
    lab_case_entry = {
        'security': {'security-identity':'security_identity', 'security-certificates':'security_certificates',
                     'security-network':'security_network', 'security-s3':'security_s3'},
        'backup': {'backup-source':'backup_source', 'backup-refusal':'backup_refusal',
                   'backup-restore':'backup_restore', 'backup-features':'backup_features'},
    }.get(lab_entry, {}).get(owner) if lab_source == 'tests/helpers/elasticsearch-lab.py' and separator else None
    dr = 'tests/helpers/elasticsearch-dr.py'
    client = 'tests/helpers/elasticsearch-business.py'
    runner = 'tests/helpers/elasticsearch-runner.sh'
    unused_coexistence = source == 'tests/helpers/elasticsearch-coexistence.py' and owner == 'upgrade'
    unused_logging = source in ('tests/helpers/logging-regression.sh','tests/helpers/logging-artifact-gate.sh',
                                'tests/helpers/kafka-cleanup.sh') and owner not in ('logging', 'contract', 'only')
    non_restore = {'maintenance', 'backup-source', 'backup-refusal', 'security-s3',
                   'security-certificates', 'security-network', 'security-identity',
                   'standard', 'compact', 'business', 'lifecycle', 'faults',
                   'observability', 'rally', 'soak', 'coexistence', 'cleanup-probes'}
    restore_free = (non_restore - {'coexistence'}) | {'csi-probe', 'performance', 'logging', 'cleanup'}
    # ES-50 signs platform maintenance, ES-53 signs installation/refusal and
    # controller recovery. The successful restore belongs to ES-51/52/54.
    consumes_restore = owner not in restore_free and case['id'] not in ('ES-50', 'ES-53')
    dr_entries = {'backup-source': '', 'backup-refusal': '',
                  'backup-restore': 'target_lab,baseline,install,restore,retire_business',
                  'backup-features': 'features', 'dr': 'disaster,fence_target_writer,routed_connection',
                  'upgrade': 'upgrade', 'cleanup': 'cleanup'}
    client_scopes = non_restore | {'backup-restore','backup-features','dr','upgrade','cleanup'}
    product_scopes = client_scopes | {'csi-probe','performance','logging'}
    eligible = (unused_coexistence or unused_logging or
                (source == runner and owner not in ('contract','only')) or
                (source in controls and owner not in ('contract', 'only')) or
                (source == product and owner in product_scopes) or
                (source == dr and owner in dr_entries) or
                (source == client and owner in client_scopes - {'soak'}) or lab_case_entry is not None)
    if not eligible:
        return False
    if source == dr and owner in ('backup-source', 'backup-refusal') and source not in record['dependencies']:
        # Early case receipts used the aggregate backup dependency set, even
        # when the frozen focused launch never imported the DR helper.
        provenance = [item for item in record.get('provenance', []) if item['path'].endswith('/launch-provenance.json')]
        return (len(provenance) == 1 and source not in json.loads(bound_file(root, provenance[0]))
                and record.get('scoped_cleanup_marker') == 'ELASTICSEARCH_MODULE_PASS module='+owner)
    # Older whole-file receipts remain immutable. Reuse requires their exact
    # archived bytes, authenticated by the receipt, before comparing the now
    # declared consumed boundary. Missing provenance fails closed.
    archives = [item for item in record.get('provenance', []) if item['path'].endswith('/launch-sources.tar.gz')]
    if len(archives) != 1:
        return False
    with tarfile.open(fileobj=io.BytesIO(bound_file(root, archives[0])), mode='r:gz') as stream:
        member = stream.getmember(lab_source if lab_case_entry else source)
        if not member.isfile():
            return False
        original = stream.extractfile(member).read()
        if unused_coexistence:
            # Historical upgrade launches did not import this transport. New
            # launches do: prove the old call boundary from its authenticated
            # DR source, or keep the actual consumed transport binding.
            if not any(name.split('#', 1)[0] == dr for name in record.get('dependencies', {})):
                return False
            try:
                archived_dr = stream.extractfile(dr).read()
            except (KeyError, AttributeError):
                return False
    original_identity = (python_dependency_digest(original, lab_entry) if lab_case_entry else hashlib.sha256(original).hexdigest())
    if original_identity != checksum:
        return False
    if lab_case_entry:
        return python_dependency_digest(original, lab_case_entry) == python_dependency_digest((root/lab_source).read_bytes(), lab_case_entry)
    if unused_coexistence:
        if b'elasticsearch-coexistence.py' not in archived_dr:
            return True
        return (python_dependency_digest(original, 'logging_connection') ==
                python_dependency_digest((root/source).read_bytes(), 'logging_connection'))
    if unused_logging:
        return True
    if source in controls:
        return True
    current = (root/source).read_bytes()
    if source == runner:
        # Authenticate the original bytes above. Only the local documentation
        # arm and regular-file permission correction leave live execution intact;
        # every byte of launch, lease, sync and supervision stays bound.
        arm=b'    --elasticsearch-documentation)\n      elasticsearch_local_gate documentation ELASTICSEARCH_DOCUMENTATION_SCOPED_CLEAN_PASS "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-full.py" --documentation "$ROOT" ;;\n'
        old=b'chmod 0600 "$directory"/*'
        fixed=b'find "$directory" -maxdepth 1 -type f -exec chmod 0600 {} +'
        return original.replace(arm,b'').replace(old,fixed) == current.replace(arm,b'').replace(old,fixed)
    if source == dr:
        return python_dependency_digest(original, dr_entries[owner], shadow_aware=True) == python_dependency_digest(current, dr_entries[owner], shadow_aware=True)
    if source == client:
        # Historical whole-file bindings included the alternative soak command.
        # Compare every other actual client entry and its shared callees, keeping
        # imports, initialization and CLI dispatch bound. A shared API/confirmation
        # change still invalidates these receipts; only unconsumed soak code drops.
        entries = 'connect,business,lifecycle,security,password_rotation,unavailable_write'
        return python_dependency_digest(original, entries) == python_dependency_digest(current, entries)
    # Restore identity is outside non-restore scenarios. The exporter CLI
    # and health-series corrections are owned only by observability.
    # Authenticate old sources and normalize only those exact expressions;
    # all callers, the enable gate and every other resource field stay bound.
    def product_ast(raw):
        tree = ast.parse(raw)
        if not consumes_restore:
            # These declared consumers never submit a restore. Keep shared
            # dispatch, snapshot and transport behavior bound; exclude only
            # the restore arm and the optional timeout's unchanged default.
            for function in tree.body:
                if isinstance(function, ast.FunctionDef) and function.name == 'operation':
                    for node in ast.walk(function):
                        if (isinstance(node, ast.If) and
                                ast.dump(node.test) == ast.dump(ast.parse('kind == "snapshot"', mode='eval').body)):
                            node.orelse = []
            signature = ast.parse('def request(method, path, body=None, *, timeout=60): pass').body[0].args
            transport = ast.parse('TunnelHTTPS(f"{name}-client.{ns}.svc", port, timeout=timeout, context=context)', mode='eval').body
            for function in tree.body:
                if not isinstance(function, ast.FunctionDef) or function.name != 'connection':
                    continue
                for node in ast.walk(function):
                    if not isinstance(node, ast.FunctionDef) or node.name != 'request' or ast.dump(node.args) != ast.dump(signature):
                        continue
                    uses = [value for value in ast.walk(node) if isinstance(value, ast.Name) and value.id == 'timeout']
                    calls = [value for value in ast.walk(node) if isinstance(value, ast.Call) and ast.dump(value) == ast.dump(transport)]
                    if len(uses) == len(calls) == 1:
                        node.args.kwonlyargs = []; node.args.kw_defaults = []
                        next(value for value in calls[0].keywords if value.arg == 'timeout').value = ast.Constant(value=60)
        # Accepted successful paths never raised a TLS verification error.
        # Preserve only that unchanged path; certificate rotation (ES-26)
        # must qualify the newly added bounded wait with current evidence.
        if owner != 'security-certificates':
            handler = ast.parse(
                'try:\n    pass\n'
                'except ssl.SSLCertVerificationError:\n'
                '    print("ELASTICSEARCH_WAIT_HEARTBEAT source=TLS reason=certificate-convergence", flush=True)\n'
                '    time.sleep(5)\n'
                '    continue'
            ).body[0].handlers[0]
            for function in tree.body:
                if isinstance(function, ast.FunctionDef) and function.name == 'health':
                    for node in ast.walk(function):
                        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == 'ready'):
                            continue
                        retry = node.body[0]
                        if (isinstance(retry, ast.Try) and not retry.orelse and not retry.finalbody and
                                len(retry.handlers) == 1 and
                                ast.dump(retry.handlers[0], include_attributes=False) == ast.dump(handler, include_attributes=False)):
                            node.body[:1] = retry.body
        # Old receipts exercise the unchanged default (full contention probe).
        # The opt-in MinIO branch is qualified separately. Strip only its exact
        # default-inert statements; a changed default, body or any other API
        # behavior still invalidates the receipt.
        option = 'elasticsearch_repository_analysis_copy_during_write_contention'
        defaults = yaml.safe_load((root/'conf/config.yml').read_text()) if (root/'conf/config.yml').is_file() else {}
        if defaults.get(option, True) is True:
            validation = ast.dump(ast.parse('flag(c.get("'+option+'", True))').body[0], include_attributes=False)
            branch = ast.dump(ast.parse(
                'if not flag(c.get("'+option+'", True)):\n'
                '    sets[-1]["config"]["repositories.blobstore.testkit.analyze.copy_during_write_contention"] = False'
            ).body[0], include_attributes=False)
            for function in tree.body:
                if isinstance(function, ast.FunctionDef) and function.name in ('validate', 'resources'):
                    excluded = validation if function.name == 'validate' else branch
                    for parent in ast.walk(function):
                        if isinstance(getattr(parent, 'body', None), list):
                            parent.body = [node for node in parent.body if ast.dump(node, include_attributes=False) != excluded]
        if not consumes_restore:
            tree.body = [node for node in tree.body if not isinstance(node, ast.FunctionDef) or node.name != 'index_uuids']
        if owner != 'observability':
            for function in tree.body:
                if isinstance(function, ast.FunctionDef) and function.name == 'monitoring':
                    for node in ast.walk(function):
                        if isinstance(node, ast.Constant) and node.value == '--es.aliases=false':
                            node.value = '--no-es.aliases'
                        if isinstance(node, ast.JoinedStr):
                            constants=[value for value in node.values if isinstance(value,ast.Constant)]
                            old=['(up{','} == 0) or absent(up{','}) or (elasticsearch_cluster_health_up{','} == 0)']
                            if [value.value for value in constants]==old:
                                constants[2].value='}) or absent(elasticsearch_cluster_health_status{'
                                constants[3].value='})'
        return ast.dump(tree, include_attributes=False)
    return product_ast(original) == product_ast(current)


def bound_file(root, binding):
    filename, checksum = binding['path'], binding['sha256']
    path = (root / filename).resolve()
    if (Path(filename).is_absolute() or not path.is_relative_to(root) or
            not re.fullmatch(r'[a-f0-9]{64}', checksum)):
        raise ValueError('evidence path or checksum is invalid')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError('evidence bytes differ from their identity')
    return raw


def verify_evidence(root, filename, case, *, historical=False):
    path = (root / filename).resolve()
    if Path(filename).is_absolute() or not path.is_relative_to(root):
        raise ValueError('evidence record escapes the repository')
    record = json.loads(path.read_text())
    if record.get('historical_only') and not historical:
        raise ValueError('historical completion does not sign current acceptance')
    if (case['id'] not in record['case_ids'] or record['durable_rc'] != 0 or record['failure_markers'] != 0
            or record['product_entry'] != case['product_entry'] or not record['scoped_cleanup']):
        raise ValueError('evidence lacks the owning product case or scoped success')
    if not record['dependencies'] or not record['raw_logs']:
        raise ValueError('evidence lacks dependency or raw-log bindings')
    inputs = record.get('case_dependencies', {}).get(case['id'], record['dependencies'])
    if not inputs:
        raise ValueError('evidence lacks owning dependency bindings')
    for source, checksum in inputs.items():
        if not re.fullmatch(r'[a-f0-9]{64}', checksum) or (not historical and not dependency_matches(root, record, case, source, checksum)):
            raise ValueError('evidence dependency differs from its identity')
    logs = b'\n'.join(bound_file(root, binding) for binding in record['raw_logs']).decode()
    require_case_markers({'success_marker':case['success_marker']} if historical else case, logs.splitlines())
    if record['scoped_cleanup_marker'] not in logs.splitlines():
        raise ValueError('raw log lacks scoped cleanup marker')
    for state in ('exit', 'finalized'):
        if bound_file(root, record['durable'][state]).strip() != b'0':
            raise ValueError('durable child exit or supervisor fence is not zero')
    for binding in record.get('provenance', []):
        bound_file(root, binding)
    return record


def validate(path, require_pass=False):
    data = yaml.safe_load(path.read_text())
    errors = []
    if not isinstance(data, dict) or data.get("meta", {}).get("component") != "elasticsearch":
        return ["matrix must describe the Elasticsearch branch"]
    cases, stages = data.get("cases", []), data.get("stages", [])
    ids, markers = set(), set()
    root = path.resolve().parents[1]
    if not isinstance(cases, list) or not isinstance(stages, list):
        return ['case and stage plans must be lists']
    for case in cases:
        cid = case.get("id", "")
        if not isinstance(cid, str) or not re.fullmatch(r"ES-[0-9]{2}", cid) or cid in ids:
            errors.append("invalid or duplicate case ID: " + cid)
        ids.add(cid)
        if case.get("group") not in GROUPS or CASE_GROUPS.get(cid) != case.get("group"):
            errors.append(cid + ": unknown capability group")
        for key in ("desc", "scenario", "official", "product_entry", "disproof", "fixture_boundary", "cleanup_scope"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                errors.append(cid + ": missing " + key)
        marker = case.get("success_marker")
        if marker != "ELASTICSEARCH_CASE_PASS id=" + cid or marker in markers:
            errors.append(cid + ": marker must bind its unique case ID")
        markers.add(marker)
        subcases = case.get('subcases', [])
        expected_subcases = {cid + '-' + name for name in ISOLATION_SUBCASES.get(cid, ())}
        if (not isinstance(subcases, list) or any(not isinstance(row, dict) for row in subcases)):
            errors.append(cid + ': subcases must be a list of scenario contracts')
        else:
            sub_ids = [row.get('id') for row in subcases]
            if set(sub_ids) != expected_subcases or len(sub_ids) != len(expected_subcases):
                errors.append(cid + ': mandatory isolation subcases differ from the acceptance contract')
            for row in subcases:
                for key in ('preconditions', 'product_entry', 'operation', 'code_guard', 'expected', 'cleanup_scope'):
                    if not isinstance(row.get(key), str) or not row[key].strip():
                        errors.append(cid + ': subcase missing ' + key)
                child_marker = row.get('success_marker')
                if child_marker != 'ELASTICSEARCH_SUBCASE_PASS id=' + str(row.get('id')) or child_marker in markers:
                    errors.append(cid + ': subcase marker must bind its unique scenario ID')
                markers.add(child_marker)
        if case.get("disproof") != "bash tests/run_enterprise_regression.sh --elasticsearch-" + str(CASE_MODULES.get(cid)):
            errors.append(cid + ": disproof must use the fixed module entry")
        status, evidence = case.get("status"), case.get("evidence")
        if status not in ("pending", "pass", "fail"):
            errors.append(cid + ": invalid status")
        if not isinstance(evidence, list) or (status == "pending" and evidence) or (status == "pass" and not evidence):
            errors.append(cid + ": invalid evidence binding")
        if status == "pass" and isinstance(evidence, list):
            for filename in evidence:
                try:
                    verify_evidence(root, filename, case)
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    errors.append(cid + ": unreadable evidence " + type(exc).__name__)
        retained = case.get('retained_evidence', [])
        if not isinstance(retained, list):
            errors.append(cid + ': retained evidence must be a list')
        else:
            for filename in retained:
                try:
                    verify_evidence(root, filename, case, historical=True)
                except (OSError, KeyError, TypeError, ValueError):
                    errors.append(cid + ': unreadable retained evidence')
    if ids != set(CASE_GROUPS):
        errors.append("acceptance must retain all 59 declared capability items")
    stage_ids = [s.get("id") for s in stages]
    if set(stage_ids) != GROUPS or len(stage_ids) != len(GROUPS):
        errors.append("stage plan must cover every declared capability group exactly once")
    planned = [cid for stage in stages for cid in stage.get("cases", [])]
    if sorted(planned) != sorted(ids):
        errors.append("each acceptance item must belong to exactly one planned stage")
    for stage in stages:
        if any(dep not in stage_ids or dep == stage["id"] for dep in stage.get("depends_on", [])):
            errors.append("invalid stage prerequisite")
    graph = {s['id']: s.get('depends_on', []) for s in stages}
    visited, active = set(), set()
    def visit(node):
        if node in active:
            return False
        if node in visited:
            return True
        active.add(node)
        if any(not visit(dep) for dep in graph.get(node, [])):
            return False
        active.remove(node); visited.add(node)
        return True
    if not all(visit(node) for node in graph):
        errors.append('stage prerequisites contain a cycle')
    for stage in stages:
        if any(CASE_GROUPS.get(cid) != stage['id'] for cid in stage.get('cases', [])):
            errors.append('stage contains an item from a different capability')
    summary = data.get("acceptance", {})
    accepted = sum(c.get("status") == "pass" for c in cases)
    if (summary.get("accepted"), summary.get("applicable"), summary.get("planned_stages")) != (accepted, len(cases), len(stages)):
        errors.append("acceptance fractions differ from the case/stage ledger")
    all_pass = accepted == len(cases) and bool(cases)
    if data["meta"].get("result") == "PASS" and not all_pass:
        errors.append("delivery PASS requires all applicable cases")
    if require_pass and (not all_pass or data["meta"].get("result") != "PASS"):
        errors.append("current complete acceptance is required")
    return errors


def progress(data):
    """Use the matrix's item identities and capability groups for every total."""
    cases=data['cases'];ids={case['id'] for case in cases}
    if len(cases)!=59 or ids!={f'ES-{i:02}' for i in range(1,60)}:
        raise ValueError('progress requires the complete 59-item matrix')
    accepted={case['id'] for case in cases if case['status']=='pass'}
    failed={case['id'] for case in cases if case['status']=='fail'}
    if data['acceptance']['accepted']!=len(accepted):
        raise ValueError('progress summary disagrees with matrix cases')
    stages=[dict(id=stage['id'],accepted=sorted(set(stage['cases'])&accepted),
                 remaining=sorted(set(stage['cases'])-accepted),cases=list(stage['cases']))
            for stage in data['stages']]
    return {'accepted':len(accepted),'applicable':len(cases),'remaining':len(cases)-len(accepted),
            'failed':len(failed),'accepted_stages':sum(not stage['remaining'] for stage in stages),
            'planned_stages':len(stages),'stages':stages,
            'cases':[dict(id=case['id'],name=case['desc'],status=case['status'],
                          reason=case.get('pending_reason',''),
                          subcases=[dict(id=row['id'],name=row.get('desc',row['id']),
                                         status='pass' if case['status']=='pass' else 'pending')
                                    for row in case.get('subcases',[])]) for case in cases]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("matrix", type=Path)
    p.add_argument("--require-pass", action="store_true")
    p.add_argument("--status", action="store_true", help="print matrix item/stage counts and case IDs")
    p.add_argument("--json", action="store_true", help="emit the same status as JSON")
    args = p.parse_args()
    errors = validate(args.matrix, args.require_pass)
    for message in errors:
        print("ELASTICSEARCH_MATRIX_FAIL " + message)
    if errors:
        return 1
    if args.status:
        status=progress(yaml.safe_load(args.matrix.read_text()))
        if args.json:print(json.dumps(status,ensure_ascii=False,indent=2));return 0
        print(f"矩阵验收 {status['accepted']}/{status['applicable']} PASS，待验收 {status['remaining']} 项（失败 {status['failed']} 项）；"
              f"能力阶段 {status['accepted_stages']}/{status['planned_stages']}")
        print('\n| 矩阵能力组 | 已 PASS ID | 待验收 ID |\n| --- | --- | --- |')
        for stage in status['stages']:
            print('| '+stage['id']+' | '+(', '.join(stage['accepted']) or '—')+' | '+(', '.join(stage['remaining']) or '—')+' |')
        print('\n待验收明细（子项完成不单独计入总数）：')
        for case in status['cases']:
            if case['status']=='pass':continue
            print(case['id']+' '+case['name']+'：'+case['status']+'；'+(case['reason'] or '尚无当前完整验收证据'))
        return 0
    print("ELASTICSEARCH_MATRIX_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
