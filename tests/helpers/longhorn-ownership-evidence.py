#!/usr/bin/env python3
"""Read complete ownership stages without replaying or rewriting their evidence."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys

sys.dont_write_bytecode = True

STAGES = (
    'ownership-csi-refusal-qualification',
    'ownership-preinstall-refusal-qualification', 'install',
    'ownership-class-refusal-qualification',
    'ownership-controller-refusal-qualification',
    'ownership-request-refusal-qualification',
)
VERIFIERS = {
    'original777': ('tests/helpers/longhorn-ownership-aggregate.sh',
                    'a8b895ecd921dc50adbe0670cb50709bc1da4d7959699307cbfac71d756d5c75'),
    'continuation782': ('tests/helpers/longhorn-parallel.py',
                        '0452433bc62bd09bb5c3833d44e377df8410d66fe58cdb82836e539c6e37d48f'),
}
PRODUCT_PREFIXES = ('common/', 'controller/', 'model/', 'playbooks/',
                    'roles/longhorn/', 'roles/cluster-addon/', 'service/')
PRODUCT_FILES = {'conf/config.yml', 'kubecli.py', 'runtime_hook.py',
                 'requirements', 'requirements-control.txt'}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def safe_path(root, name):
    relative = Path(name)
    require(not relative.is_absolute() and '..' not in relative.parts, 'unsafe evidence path')
    require(not root.is_symlink(), 'symlink evidence root')
    path = root
    for part in relative.parts:
        path = path / part
        require(not path.is_symlink(), 'symlink evidence path')
    return path


def checked(path, digest):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode), 'evidence is not a regular file')
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == digest, 'evidence SHA256 differs: ' + str(path))
    return raw


def product_map(inputs):
    return {key: value for key, value in inputs.items()
            if key.startswith(PRODUCT_PREFIXES) or key in PRODUCT_FILES}


def check_continuity(data, raw_json, logs):
    continuity = data['baseline_continuity']
    capture = next(value for value in raw_json.values() if 'baseline' in value)
    baseline = capture['baseline']
    owner = baseline['owner']
    require(continuity['owner'] == owner and owner['healthy'] is True and
            owner['clean_start_verified'] is True and len(owner['node_uids']) == 6,
            'fixture identity or health differs')
    prior = raw_json[continuity['prior_run_id_evidence']['path']]
    classes = prior['rendered_summaries']['yml/longhorn/input.json']['nonsecret_fields']['longhorn_storage_classes']
    require('longhorn-owner-' + owner['run_id'] in {item['name'] for item in classes},
            'original fixture run identity differs')
    require(all(value.get('exists') is False and not value.get('symlink', False)
                for value in baseline['ownership_journals'].values()) and
            len(baseline['ownership_journals']) == 3, 'ownership journal remains')
    require(baseline['shared_context']['sha256'] == continuity['shared_context_sha256_preserved'],
            'shared context differs')
    contexts = [value for value in raw_json.values() if set(value) == {'exists', 'mode', 'sha256', 'uid'}]
    require(len(contexts) == 1 and contexts[0]['sha256'] == baseline['shared_context']['sha256'] and
            contexts[0]['exists'] is True and contexts[0]['mode'] == 0o400,
            'shared context evidence differs')
    require(len(owner['disks']) == 3, 'installed disk set differs')
    for host, disk in owner['disks'].items():
        line = 'LONGHORN_LAB_DISK_READY node={} uuid={} size={}'.format(host, disk['uuid'], disk['size'])
        require(disk['size'] == 107374182400 and line in logs['install'], 'installed disk identity differs')
    for stage in STAGES[2:]:
        require('LONGHORN_CORE_IDENTITY_PASS nodes=6' in logs[stage] and
                'LONGHORN_LAB_INSTALLATION_HEALTHY' in logs[stage], 'core/installation guard missing')
    fingerprint = 'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes={} sha256={} '.format(
        continuity['request_confirmed_bytes'], continuity['request_confirmed_sha256'])
    require(any(line.startswith(fingerprint) for line in logs['install']) and
            any(line.startswith(fingerprint) for line in logs[STAGES[-1]]),
            'request data continuity differs')


def consume(root, request, digest, archives):
    data = json.loads(checked(request, digest))
    require(data['case'] == 'LONGHORN-03' and data['version'] == '1.12.1' and
            data['instance_manager_version'] == 'v1.12.1-kubeauto.2', 'ownership version differs')
    require(set(data['stages']) == set(STAGES), 'complete six-stage set required')
    require(set(data['input_sets']) == set(VERIFIERS), 'two frozen input sets required')
    inputs = {}
    for name, (source, source_sha) in VERIFIERS.items():
        record = data['input_sets'][name]
        archive = archives[name]
        manifest = archive / 'frozen-inputs.json'
        inputs[name] = json.loads(checked(manifest, record['manifest_sha256']))
        require(inputs[name] == record['frozen_inputs'] and
                len(inputs[name]) == record['verified_before_and_after'] == int(name[-3:]),
                'frozen input manifest differs')
        for path, sha in inputs[name].items():
            checked(safe_path(archive / 'frozen-source', path), sha)
        require(inputs[name][source] == source_sha, 'owning verifier source differs')
    dependencies = data['unchanged_product_execution_dependencies']
    require(len(dependencies) == 106 and all(product_map(value) == dependencies for value in inputs.values()),
            'product dependency closure differs')
    for path, sha in dependencies.items():
        checked(safe_path(root, path), sha)
    raw = data['raw_evidence']
    raw_json = {}
    for path, sha in raw.items():
        value = checked(safe_path(root, path), sha)
        if path.endswith('.json'):
            raw_json[path] = json.loads(value)
    logs = {}
    results = []
    for stage in STAGES:
        record = data['stages'][stage]
        name = 'original777' if stage in STAGES[:4] else 'continuation782'
        source, source_sha = VERIFIERS[name]
        require(record['input_set'] == name and record['exit'] == record['finalized'] ==
                record['failure_markers'] == 0, 'stage did not succeed')
        require(record['owning_verifier']['path'] == source and
                record['owning_verifier']['sha256'] == source_sha, 'stage verifier differs')
        paths = record['raw']
        require(len(paths) == 4 and all(raw.get(path) == sha for path, sha in paths.items()),
                'stage raw binding differs')
        expected = stage if name == 'original777' else 'v1'
        require({Path(path).name for path in paths} ==
                {expected + suffix for suffix in ('.exit', '.finalized', '.log', '.pid')},
                'stage durable file names differ')
        directories = {safe_path(root, path).parent for path in paths}
        require(len(directories) == 1, 'stage raw directories differ')
        directory = directories.pop()
        log = directory / (expected + '.log')
        logs[stage] = log.read_text().splitlines()
        frozen = archives[name] / 'frozen-source' / source
        if name == 'original777':
            result = subprocess.run(['bash', '-c',
                'source "$1"; PY="$2"; verify_stage "$3" "$4"',
                'ownership-evidence', str(frozen), sys.executable, str(directory), stage],
                text=True, capture_output=True, timeout=30, check=True)
            require(result.stdout.strip() == record['original_frozen_verifier'],
                    'original stage verifier result differs')
            result = {'stage': stage, 'verifier_output': result.stdout.strip()}
        else:
            require(directory.name == stage and record['owning_verifier']['branch'] == stage,
                    'parallel owning branch differs')
            spec = importlib.util.spec_from_file_location('ownership_frozen_parallel', frozen)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.verify_branch(directory, stage)
            require(result == record['verification'] and
                    logs[stage].count(record['outer_exit_marker']) == 1,
                    'parallel verifier result differs')
        results.append(result)
    capture = next(value for value in raw_json.values() if 'baseline' in value)
    require(capture['frozen_inputs'] == inputs['continuation782'] and
            capture['source_map_sha256'] == data['input_sets']['continuation782']['manifest_sha256'] and
            capture['verified_before_and_after'] == 782, 'capture input binding differs')
    check_continuity(data, raw_json, logs)
    return {'case': 'LONGHORN-03', 'request_sha256': digest, 'stages': results,
            'product_dependencies': len(dependencies), 'raw_files': len(raw),
            'old_failed_parent_promoted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--original-archive', type=Path, required=True)
    parser.add_argument('--continuation-archive', type=Path, required=True)
    args = parser.parse_args()
    result = consume(args.root.resolve(), args.request, args.sha256,
                     {'original777': args.original_archive, 'continuation782': args.continuation_archive})
    print('LONGHORN_OWNERSHIP_EVIDENCE_IMPORTED ' + json.dumps(result, sort_keys=True))
    print('LONGHORN_OWNERSHIP_SCOPED_CLEAN_PASS')
    print('LONGHORN_OWNERSHIP_PASS')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
        print('LONGHORN_OWNERSHIP_EVIDENCE_FAIL reason=' + type(error).__name__, file=sys.stderr)
        raise
