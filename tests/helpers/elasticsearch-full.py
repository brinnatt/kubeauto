#!/usr/bin/env python3
"""One clean, leased delivery run composed from the qualified fixed modules."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys

import yaml

BASE=Path('/usr/local/kubeauto')


def load(name):
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),BASE/'tests/helpers'/name)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def documentation(root):
    receipt=json.loads((root/'tests/evidence/elasticsearch-documentation.json').read_text())
    expected={'docs/middleware/elasticsearch/'+name for name in ('operations-manual.md','technical-whitepaper.md','development-manual.md')}
    checks={'sources','content','novice_path','operator_path','diagrams'}
    if (receipt.get('status')!='pass' or receipt.get('blocking_defects')!=[] or set(receipt.get('files',{}))!=expected or
            receipt.get('versions')!={'elasticsearch':'9.5.6','eck':'3.5.0','kubernetes':'1.33.6'} or
            any(receipt.get('review',{}).get(check)!='pass' for check in checks)):
        raise ValueError('current author documentation review is incomplete')
    for name,checksum in receipt['files'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=checksum:raise ValueError('reviewed documentation changed')
    print('ELASTICSEARCH_CASE_PASS id=ES-58',flush=True)
    print('ELASTICSEARCH_DOCUMENTATION_REVIEW_PASS reviewer=author customer_signoff=false',flush=True)


def completed_stages(matrix,cases):
    return sum(set(stage['cases'])<=cases for stage in matrix['stages'])


def report_progress(lab,directory,matrix,cases,module,run_id):
    """Persist only this run's growing set of accepted cases, never a supplied count."""
    if not re.fullmatch('[a-f0-9]{32}',run_id) or directory.name!='full-'+run_id:
        raise ValueError('progress requires its own full run identity')
    known={case['id'] for case in matrix['cases']}
    if not cases<=known:raise ValueError('progress contains unknown matrix cases')
    state={'run_id':run_id,'scope':'full','case_ids':sorted(cases),
           'applicable_cases':len(known),'planned_stages':len(matrix['stages']),
           'accepted_stages':completed_stages(matrix,cases),'module':module}
    path=directory/'progress.json'
    if path.exists():
        previous=json.loads(path.read_text())
        for key in ('run_id','scope','applicable_cases','planned_stages'):
            if previous.get(key)!=state[key]:raise ValueError('progress run or scope changed: '+key)
        prior=set(previous['case_ids'])
        if not prior<=cases:raise ValueError('progress would discard accepted cases')
        if previous['accepted_stages']!=completed_stages(matrix,prior):
            raise ValueError('progress stage count disagrees with its accepted cases')
    lab.atomic(path,state)
    accepted={case['id'] for case in matrix['cases'] if case['status']=='pass'}
    print('ELASTICSEARCH_FULL_PROGRESS run_id='+run_id+' scope=full matrix_cases='+str(len(accepted))+'/'+str(len(known))+
          ' capability_stages='+str(completed_stages(matrix,accepted))+'/'+str(state['planned_stages'])+
          ' final_completed_steps='+str(state['accepted_stages'])+'/'+str(state['planned_stages']),flush=True)


def module_cases(raw,module,scopes,matrix):
    lines=raw.splitlines()
    terminal=('ELASTICSEARCH_DR_PLATFORM_PASS independent_kubernetes=true' if module=='dr-bootstrap'
              else 'ELASTICSEARCH_MODULE_PASS module='+module)
    if terminal not in lines or any(
            re.match(r'^(?:ELASTICSEARCH_(?:FIRST_FAILURE|[A-Z_]*FAIL)\b|Traceback|\[FAIL\])',line) for line in lines):
        raise ValueError('module lacks its current complete success boundary: '+module)
    found = {line.removeprefix('ELASTICSEARCH_CASE_PASS id=') for line in lines
             if line.startswith('ELASTICSEARCH_CASE_PASS id=')} & scopes.get(module,set())
    for case in matrix['cases']:
        if case['id'] in found:
            expected = {row['success_marker'] for row in case.get('subcases', [])}
            if not expected <= set(lines):
                raise ValueError('module lacks mandatory isolation subcase markers: '+case['id'])
    return found


def run_module(lab, evidence, directory, module, environment):
    """Keep each concurrently supervised branch's real log and exit fences."""
    matrix=yaml.safe_load((BASE/'tests/elasticsearch-test-matrix.yaml').read_text())
    stem=directory/module
    log=directory/(module+'.log')
    checkpoint=None
    if evidence.SCOPES.get(module):
        run_id=environment['ELASTICSEARCH_RUN_ID']
        if not re.fullmatch('[a-f0-9]{32}',run_id):raise ValueError('child evidence requires its parent run identity')
        checkpoint=BASE/'logs/elasticsearch-cases'/('full-child-'+run_id+'-'+module)
        checkpoint.mkdir(mode=0o700,parents=True,exist_ok=True)
        evidence.freeze(BASE,checkpoint,module)
    print('ELASTICSEARCH_FULL_MODULE_BEGIN module='+module,flush=True)
    if module=='dr-bootstrap':
        command=[BASE/'.venv/bin/python',BASE/'tests/helpers/elasticsearch-dr.py','bootstrap']
        cluster='elasticsearch-dr'
    else:
        command=['bash',BASE/'tests/helpers/elasticsearch-gate.sh',module]
        cluster='elasticsearch-gate'
    child_environment=dict(environment,KUBECONFIG=str(BASE/'clusters'/cluster/'kubectl.kubeconfig'))
    rc=lab.stream_command(['bash',BASE/'tests/helpers/run-durable-gate.sh',stem,'ELASTICSEARCH_FULL_CHILD_EXIT',
                          *command],log,child_environment,28800)
    if rc or any(Path(str(stem)+suffix).read_text().strip()!='0' for suffix in ('.exit','.finalized')):
        raise ValueError('full child failed; preserve its first diagnostics: '+module)
    found=module_cases(log.read_text(),module,evidence.SCOPES,matrix)
    if checkpoint is not None:
        shutil.copyfile(log,checkpoint/'raw.log')
        for name in ('exit','finalized'):shutil.copyfile(Path(str(stem)+'.'+name),checkpoint/name)
        for path in checkpoint.iterdir():path.chmod(0o600)
        if found:
            published=evidence.publish(BASE,checkpoint,'ELASTICSEARCH_MODULE_PASS module='+module,module)
            if set(published)!=found:raise ValueError('child evidence did not accept its complete case set: '+module)
            print('ELASTICSEARCH_CHILD_EVIDENCE_PASS module='+module,flush=True)
    return found


def platform_parallel(lab, evidence, directory, environment, source_modules, accepted):
    """Prepare the independent DR pool while progressing healthy source gates.

    The caller already owns both pools and the control context. Source gates
    remain serialized because they share config.yml, ECK and business data.
    Neither branch may delete the other's hosts or state. Joining both futures
    preserves a completed independent branch when its sibling fails.
    """
    lab.baseline()
    dr=load('elasticsearch-dr.py')
    if set(lab.HOSTS)&set(dr.HOSTS):
        raise ValueError('parallel source and DR hosts overlap')
    if environment.get('ELASTICSEARCH_CONTEXT_OWNER_RUN_ID')!=environment.get('ELASTICSEARCH_RUN_ID') or not environment.get('ELASTICSEARCH_RUN_ID'):
        raise ValueError('parallel branches require the parent-owned control context')
    errors=[]
    with ThreadPoolExecutor(max_workers=1) as pool:
        target=pool.submit(run_module,lab,evidence,directory,'dr-bootstrap',environment)
        try:
            for module in source_modules:
                accepted(module,run_module(lab,evidence,directory,module,environment))
        except Exception as error:
            errors.append(error)
        try:
            accepted('dr-bootstrap',target.result())
        except Exception as error:
            errors.append(error)
    if errors:
        raise errors[0]
    print('ELASTICSEARCH_PARALLEL_PLATFORM_PASS source=verified dr=verified branches=2',flush=True)


def qualify_platform():
    """The narrow live qualification before the complete composed run."""
    if os.environ.get('ELASTICSEARCH_PARENT_LEASE_HELD')!='yes':
        raise ValueError('fixed parent lease required')
    run_id=os.environ.get('ELASTICSEARCH_RUN_ID','')
    if not re.fullmatch('[a-f0-9]{32}',run_id):raise ValueError('bound run identity required')
    lab=load('elasticsearch-lab.py')
    evidence=load('elasticsearch-evidence.py')
    lab.baseline()
    if not lab.api().get('storageclass','es-test'):
        raise ValueError('parallel platform qualification requires the source product StorageClass')
    directory=lab.STATE.parent/'kubeauto-elasticsearch-history'/('parallel-platform-'+run_id)
    directory.mkdir(mode=0o700,parents=True)
    environment=dict(os.environ,ELASTICSEARCH_CONTEXT_OWNER_RUN_ID=run_id)
    platform_parallel(lab,evidence,directory,environment,('csi-probe',),lambda module,cases:None)
    print('ELASTICSEARCH_PARALLEL_PLATFORM_SCOPED_CLEAN_PASS source=owned-healthy target=owned-healthy',flush=True)


def main():
    if os.environ.get('ELASTICSEARCH_PARENT_LEASE_HELD')!='yes':raise ValueError('fixed parent lease required')
    run_id=os.environ.get('ELASTICSEARCH_RUN_ID','')
    if not re.fullmatch('[a-f0-9]{32}',run_id):raise ValueError('bound run identity required')
    lab=load('elasticsearch-lab.py');evidence=load('elasticsearch-evidence.py')
    prerequisite=json.loads((BASE/'tests/evidence/elasticsearch-full-prerequisites.json').read_text())
    if prerequisite['dependencies']!=evidence.dependencies(BASE):raise ValueError('full prerequisites no longer match synced inputs')
    if not prerequisite['evidence']:raise ValueError('full prerequisites have no source receipts')
    for name,checksum in prerequisite['evidence'].items():
        if hashlib.sha256((BASE/name).read_bytes()).hexdigest()!=checksum:raise ValueError('full prerequisite receipt changed')
    cases=set(prerequisite['case_ids'])
    if cases!=evidence.SCOPES['contract']|evidence.SCOPES['artifacts']:raise ValueError('full prerequisites do not cover contract/publication')
    # Review must be current at launch. The live run does not claim to review
    # writing quality or invent a customer's sign-off.
    documentation(BASE)
    lab.STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    if lab.DIRECTORY.exists() or any(lab.STATE.iterdir()):raise ValueError('full run requires a clean archived source boundary')
    directory=lab.STATE.parent/'kubeauto-elasticsearch-history'/('full-'+run_id)
    directory.mkdir(mode=0o700,parents=True)
    matrix=yaml.safe_load((BASE/'tests/elasticsearch-test-matrix.yaml').read_text())
    def accepted(module, found):
        cases.update(found)
        report_progress(lab,directory,matrix,cases,module,run_id)
    environment=dict(os.environ,ELASTICSEARCH_CONTEXT_OWNER_RUN_ID=run_id)
    report_progress(lab,directory,matrix,cases,'prerequisites',run_id)
    accepted('bootstrap',run_module(lab,evidence,directory,'bootstrap',environment))
    platform_parallel(lab,evidence,directory,environment,
                      ('storage','csi-probe','standard','compact','business','lifecycle','security','faults',
                       'maintenance','observability','performance'),accepted)
    for module in ('backup','dr','upgrade','coexistence','cleanup-probes','logging','cleanup'):
        accepted(module,run_module(lab,evidence,directory,module,environment))
    documentation(BASE);cases.add('ES-58')
    if cases!=evidence.SCOPES['only']-{'ES-59'}:raise ValueError('full run has uncovered matrix items: '+','.join(sorted(evidence.SCOPES['only']-cases)))
    # Parent run-durable-gate writes its final rc only after this function and
    # the gate's context restore exit successfully; publication checks both.
    for case in sorted(cases):print('ELASTICSEARCH_CASE_PASS id='+case,flush=True)
    print('ELASTICSEARCH_CASE_PASS id=ES-59',flush=True)
    print('ELASTICSEARCH_FULL_READY run_id='+run_id+' scope=full candidate_cases=59/59 awaiting_parent_durable_exit=true',flush=True)
    print('ELASTICSEARCH_FULL_GATE_PASS',flush=True)


if __name__=='__main__':
    try:
        if len(sys.argv)==3 and sys.argv[1]=='--documentation':
            documentation(Path(sys.argv[2]).resolve())
            print('ELASTICSEARCH_DOCUMENTATION_SCOPED_CLEAN_PASS',flush=True)
        elif sys.argv[1:]==['--platform-only']:qualify_platform()
        elif sys.argv[1:]:raise ValueError('unknown composition mode')
        else:main()
    except (ValueError,KeyError,OSError) as error:
        print('ELASTICSEARCH_FULL_REFUSED reason='+str(error),file=sys.stderr);raise SystemExit(1)
