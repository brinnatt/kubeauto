#!/usr/bin/env python3
"""Accept every current clean full-run stage before publishing the 24-case result."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile

STAGES = ('clean-before', 'clean-before-verify', 'install', 'platform', 'sentinel-data',
          'sentinel-security', 'sentinel-failure', 'sentinel-quorum', 'persistence', 'backup',
          'capacity', 'observability', 'performance', 'cleanup-qualification', 'idempotence',
          'password-rotation', 'tls-rotation', 'upgrade-prepare', 'upgrade-sentinel',
          'cluster-install', 'cluster-data', 'cluster-snapshot', 'cluster-failure', 'cluster-rebalance',
          'maintenance-rolling', 'maintenance-drain', 'crd-upgrade', 'upgrade-cluster',
          'final-idempotence', 'stability', 'clean-after', 'clean-after-verify')


def verify_stages(directory, evidence):
    records = {}
    for stage in STAGES:
        path = directory / 'stages' / stage
        log = (path / 'first-run.log').read_text()
        if ((path / 'durable.exit').read_text().strip() != '0' or
                (path / 'durable.finalized').read_text().strip() != '0' or
                not re.search(r'^REDIS_GATE_EXIT rc=0$', log, re.MULTILINE) or
                re.search(r'\[FAIL\]|REDIS_[A-Z_]*(?:FAIL|FIRST_FAILURE)\b', log)):
            raise ValueError('full stage lacks durable, failure-free success: ' + stage)
        module = 'idempotence' if stage == 'final-idempotence' else stage
        evidence.frozen_dependencies(path / 'launch-source.json', module)
        if module in evidence.CASES | evidence.PARTIAL:
            evidence.verify_log(module, log, 'state=exited rc=0')
        else:
            marker = ('REDIS_LAB_CLEANUP_PASS' if stage in ('clean-before', 'clean-after') else
                      'REDIS_LAB_CLEAN_VERIFY_PASS' if stage.endswith('-verify') else
                      'REDIS_INSTALL_MODULE_PASS' if stage == 'install' else
                      'REDIS_FOCUSED_MODULE_PASS module=upgrade-prepare')
            if not any(line == marker or line.startswith(marker + ' ') for line in log.splitlines()):
                raise ValueError('full stage business marker missing: ' + stage)
        if stage == 'clean-after' and any(marker not in log.splitlines() for marker in
                ('REDIS_CLEAN_VERIFY_PASS','LAB_CLEAN_VERIFY_PASS','REDIS_LAB_CLEAN_VERIFY_PASS')):
            raise ValueError('complete owned Redis/CSI/core cleanup evidence missing')
        records[stage] = {'durable_rc': 0, 'failure_markers': 0,
                          'log_sha256': evidence.digest(path / 'first-run.log'),
                          'launch_source_sha256': evidence.digest(path / 'launch-source.json')}
    return records


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('redis_full_bound_evidence',root/'tests/helpers/redis-evidence.py')
    evidence=importlib.util.module_from_spec(spec);spec.loader.exec_module(evidence)
    log_path=args.directory/'first-run.log'
    log=log_path.read_text();state=(args.directory/'durable-status.txt').read_text()
    if (not re.search(r'\bstate=exited\b.*\brc=0\b',state) or
            re.search(r'\[FAIL\]|REDIS_[A-Z_]*(?:FAIL|FIRST_FAILURE)\b',log) or
            any(log.splitlines().count(marker)!=1 for marker in
                ('REDIS_FULL_GATE_PASS','REDIS_DELIVERY_BRANCH_PASS')) or
            log.splitlines()[-1:]!=['REDIS_GATE_EXIT rc=0']):
        raise ValueError('full-run supervisor evidence is incomplete or failed')
    manifest=args.directory/'launch-source.json'
    dependencies=evidence.frozen_dependencies(manifest,'full',root)
    if dependencies != evidence.capture_dependencies(root,'full')['dependencies']:
        raise ValueError('local delivery source differs from the frozen executed source')
    stages=verify_stages(args.directory,evidence)
    doc={'schema':1,'case_ids':[f'REDIS-{i:02d}' for i in range(1,25)],
         'acceptance':'complete clean full Redis regression','durable_rc':0,'failure_markers':0,
         'runner':'bash tests/run_enterprise_regression.sh --redis-only',
         'dependencies':dependencies,'stages':stages,'log_sha256':evidence.digest(log_path),
         'launch_source_sha256':evidence.digest(manifest),
         'cleanup_scope':'All owned Redis, CSI and core fixtures on exact leased 131-136; other clusters and registry cache preserved',
         'markers':['REDIS_FULL_GATE_PASS','REDIS_DELIVERY_BRANCH_PASS','LAB_CLEAN_VERIFY_PASS',
                    'REDIS_LAB_CLEAN_VERIFY_PASS','REDIS_CLEAN_VERIFY_PASS']}
    with args.output.open('x') as out:
        json.dump(doc,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
    matrix=root/'tests/redis-test-matrix.yaml';original=matrix.read_text();updated=original
    for case in doc['case_ids']:
        updated=evidence.matrix_with_evidence(updated,case,args.output.resolve().relative_to(root))
    updated=re.sub(r'(?m)^  result: (PENDING|PASS)$','  result: PASS',updated,count=1)
    with tempfile.NamedTemporaryFile(mode='w',dir=matrix.parent,prefix='.redis-full-matrix-',delete=False) as out:
        temporary=Path(out.name)
        try:
            out.write(updated);out.flush();os.fsync(out.fileno())
            temporary.chmod(matrix.stat().st_mode & 0o777)
            if matrix.read_text()!=original:
                raise ValueError('concurrent matrix mutation; refusing final sign-off')
            temporary.replace(matrix)
        finally:
            temporary.unlink(missing_ok=True)
    print('REDIS_FULL_EVIDENCE_PASS cases=24 durable-rc=0 failure-markers=0 clean=verified')


if __name__=='__main__': main()
