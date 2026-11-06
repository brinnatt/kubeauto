#!/usr/bin/env python3
"""Bind accepted ES cases to launch inputs, raw logs and durable exit files."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

import yaml

ROOT=Path(__file__).resolve().parents[2]
SCOPES={name:{f'ES-{i:02}' for i in numbers} for name,numbers in {
    'contract':range(1,5),'artifacts':range(5,7),'platform':range(7,10),'csi-probe':(7,9),
    'standard':(8,10,11,12),'compact':range(13,15),'business':range(15,21),
    'lifecycle':range(21,24),'security':range(24,30),'faults':range(30,35),
    'security-identity':(24,25,27),'security-certificates':(26,),
    'security-network':(29,),'security-s3':(28,),'security-certificate-restore':(),
    'maintenance':range(35,39),'backup':range(39,43),'backup-source':(39,),
    'backup-refusal':(40,),'backup-restore':(41,),'backup-features':(42,),
    'dr':range(43,46),'observability':range(46,48),'performance':(48,49),
    'rally':(48,),'soak':(49,),'upgrade':range(50,53),'upgrade-platform':(50,),'coexistence':(53,54),'coexistence-isolation':(),'coexistence-restores':(),
    'logging':(55,),'cleanup-probes':(56,),'cleanup':(57,),'documentation':(58,),'delivery':(58,),'only':range(1,60)}.items()}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validator(root):
    spec=importlib.util.spec_from_file_location('es_evidence_validator',root/'tests/helpers/validate_elasticsearch_matrix.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def dependencies(root, scope=None):
    if scope=='documentation':
        names=['docs/middleware/elasticsearch/'+name for name in ('operations-manual.md','technical-whitepaper.md','development-manual.md')]
        names+=['tests/evidence/elasticsearch-documentation.json','tests/helpers/elasticsearch-full.py',
                'tests/helpers/elasticsearch-evidence.py','tests/helpers/validate_elasticsearch_matrix.py','tests/helpers/run-durable-gate.sh']
        return {name:digest(root/name) for name in names}
    fixed=('common/constants.py','conf/config.yml','service/cluster/downloader.py',
           'service/cluster/registry.py','service/cluster/manager.py','tests/run_enterprise_regression.sh',
           'roles/cluster-addon/tasks/main.yml','roles/cluster-addon/tasks/elasticsearch.yml',
           'roles/cluster-addon/tasks/eck-platform.yml',
           'roles/cluster-addon/tasks/logging.yml','tests/unit/test_logging_delivery.py',
           'tests/unit/test_registry_pull_sources.py','tests/unit/test_six_repo_version_sync.py',
           'tests/helpers/validate_elasticsearch_matrix.py','tests/helpers/longhorn-artifacts.py',
           'tests/helpers/lab-wipe-nodes.sh','tests/helpers/kafka-cleanup.sh','tests/helpers/longhorn-lab.py',
           'tests/helpers/longhorn-node-failure.py',
           'playbooks/09.longhorn.yml','roles/cluster-addon/tasks/prometheus.yml')
    patterns=('tests/helpers/elasticsearch-*','tests/unit/test_elasticsearch*',
              'tests/helpers/logging-*.sh','tests/helpers/run-durable-gate.sh',
              'tests/fixtures/elasticsearch/*','roles/cluster-addon/files/elasticsearch.py',
              'roles/cluster-addon/files/eck.py','roles/cluster-addon/files/eck-3.5.0-*',
              'roles/cluster-addon/templates/logging/**/*','roles/cluster-addon/files/loki-18.9.0.tgz*',
              'roles/cluster-addon/files/alloy-1.11.1.tgz*','tests/evidence/elasticsearch-images-20261103.json',
              'roles/longhorn/tasks/*.yml','roles/longhorn/defaults/*.yml','roles/longhorn/files/*.py',
              'roles/longhorn/files/longhorn-1.12.1.tgz*','roles/longhorn/files/images.json',
              'roles/longhorn/files/snapshot-controller-8.6.0/*','roles/longhorn/templates/**/*',
              'roles/cluster-addon/templates/prometheus/**/*','roles/cluster-addon/files/kube-prometheus-stack-88.0.0.tgz')
    files={root/name for name in fixed}
    for pattern in patterns:files.update(p for p in root.glob(pattern) if p.is_file())
    names={str(p.relative_to(root)) for p in files}
    if scope == 'artifacts':
        names={name for name in names if name in (
            'common/constants.py','tests/helpers/elasticsearch-artifacts.py',
            'tests/helpers/longhorn-artifacts.py','tests/helpers/run-durable-gate.sh',
            'tests/helpers/elasticsearch-runner.sh','tests/helpers/elasticsearch-evidence.py',
            'tests/helpers/validate_elasticsearch_matrix.py',
            'tests/evidence/elasticsearch-images-20261103.json','roles/cluster-addon/files/eck.py')
            or name.startswith('roles/cluster-addon/files/eck-3.5.0-')}
    elif scope not in (None, 'contract', 'only'):
        names={name for name in names if not name.startswith('tests/unit/')}
        entry={'standard':'install','compact':'install','cleanup':'cleanup_gate'}.get(scope, scope.replace('-', '_'))
        lab='tests/helpers/elasticsearch-lab.py'
        check=validator(root)
        # Refuse an unknown boundary instead of silently dropping dependencies.
        check.dependency_digest(root, lab+'#'+entry)
        names.remove(lab);names.add(lab+'#'+entry)
        clients={
            'standard':(), 'compact':(), 'business':('business',), 'lifecycle':('lifecycle',),
            'security':('security','password_rotation','business'),
            'security-identity':('security','password_rotation'),
            'security-certificates':('business',), 'security-certificate-restore':(),
            'security-network':(), 'security-s3':(),
            'faults':('business','unavailable_write'), 'maintenance':('business','unavailable_write'),
            'soak':('soak',), 'rally':(), 'performance':('soak',),
        }
        client='tests/helpers/elasticsearch-business.py'
        if scope in clients:
            names.remove(client)
            if clients[scope]:names.add(client+'#'+','.join(sorted({'connect', *clients[scope]})))
        # Dynamic imports and scripts sent to Jobs are declared by consumer.
        extra={
            'elasticsearch-network.py':{'security','security-network'},
            'elasticsearch-rally.py':{'rally','performance'},
            'elasticsearch-s3.py':{'security','security-s3','backup','backup-source','backup-refusal',
                                    'backup-restore','backup-features','dr','upgrade','upgrade-platform','cleanup'},
            'elasticsearch-dr.py':{'backup','backup-restore','backup-features','dr-bootstrap','dr','upgrade','upgrade-platform','cleanup'},
            'elasticsearch-coexistence.py':{'coexistence','coexistence-isolation','coexistence-restores','upgrade','upgrade-platform'},
            'elasticsearch-filesystem.py':{'bootstrap','bootstrap-resume','storage','cleanup'},
            'elasticsearch-full.py':set(), 'elasticsearch-static.sh':set(),
            'elasticsearch-stop.py':set(),
            'logging-regression.sh':{'logging'}, 'logging-artifact-gate.sh':{'logging'},
            'kafka-cleanup.sh':{'logging'},
        }
        for filename, consumers in extra.items():
            if scope not in consumers:names.discard('tests/helpers/'+filename)
        if scope in ('upgrade','upgrade-platform'):
            name='tests/helpers/elasticsearch-coexistence.py'
            names.remove(name);names.add(name+'#logging_connection')
    check=validator(root)
    return {name:check.dependency_digest(root,name) for name in sorted(names)}


def binding(root,path):
    return {'path':str(path.resolve().relative_to(root.resolve())),'sha256':digest(path)}


def launch_dependencies(root,scope):
    result=dependencies(root,scope)
    if scope=='only':
        filename='tests/evidence/elasticsearch-full-prerequisites.json'
        result[filename]=digest(root/filename)
    return result


def freeze(root,directory,scope):
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=directory/'launch-inputs.json'
    if path.exists():raise ValueError('launch input identity already exists')
    inputs=launch_dependencies(root,scope)
    sources=sorted({name.split('#',1)[0] for name in inputs})
    path.write_text(json.dumps(inputs,sort_keys=True,indent=2)+'\n');path.chmod(0o600)
    # Keep byte provenance separately from semantic invalidation. Future impact
    # analysis can recover the exact old source instead of guessing from a hash.
    provenance=directory/'launch-provenance.json'
    provenance.write_text(json.dumps({name:digest(root/name) for name in sources},sort_keys=True,indent=2)+'\n')
    provenance.chmod(0o600)
    archive=directory/'launch-sources.tar.gz'
    with tarfile.open(archive,'x:gz') as stream:
        for name in sources:
            if Path(name).suffix in ('.py','.sh'):stream.add(root/name,arcname=name,recursive=False)
    archive.chmod(0o600)


def refresh(root):
    matrix=root/'tests/elasticsearch-test-matrix.yaml';data=yaml.safe_load(matrix.read_text());changed=[]
    check=validator(root);restored=[]
    for case in data['cases']:
        if case['status']=='pending' and case.get('pending_reason','').startswith('changed dependencies: '):
            # A rejected change may have been reverted. Rebind only this case's
            # previously accepted receipt, after the normal current-evidence
            # verifier checks all inputs, raw results, cleanup and exit fences.
            for filename in reversed(case.get('retained_evidence',[])):
                try:check.verify_evidence(root,filename,case)
                except (OSError,SyntaxError,KeyError,TypeError,ValueError,tarfile.TarError):continue
                case.update(status='pass',evidence=[filename]);case.pop('pending_reason')
                restored.append(case['id']);break
        if case['status']!='pass':continue
        affected=set()
        for filename in case['evidence']:
            record=json.loads((root/filename).read_text())
            inputs=record.get('case_dependencies',{}).get(case['id'],record['dependencies'])
            for name,checksum in inputs.items():
                try:
                    matches=check.dependency_matches(root,record,case,name,checksum)
                except (OSError, SyntaxError, KeyError, ValueError, tarfile.TarError):
                    matches=False
                if not matches:affected.add(name)
            if case.get('subcases'):
                try:
                    lines=b'\n'.join(check.bound_file(root,item) for item in record['raw_logs']).decode().splitlines()
                    check.require_case_markers(case,lines)
                except (OSError, KeyError, TypeError, ValueError):
                    affected.add('mandatory isolation subcases')
        if affected:
            case['retained_evidence']=sorted(set(case.get('retained_evidence', []))|set(case['evidence']))
            case.update(status='pending',evidence=[],pending_reason='changed dependencies: '+', '.join(sorted(affected)))
            changed.append(case['id'])
    if changed or restored:
        data['acceptance']['accepted']=sum(case['status']=='pass' for case in data['cases'])
        data['meta']['result']='PENDING';matrix.write_text(yaml.safe_dump(data,allow_unicode=True,sort_keys=False))
    if restored:print('ELASTICSEARCH_EVIDENCE_RESTORED cases='+','.join(restored),flush=True)
    return changed


def retain_history(root):
    """Keep validated historical results visible without signing changed code."""
    matrix=root/'tests/elasticsearch-test-matrix.yaml';data=yaml.safe_load(matrix.read_text())
    check=validator(root);found={}
    for path in sorted((root/'tests/evidence').glob('elasticsearch-*.json'),reverse=True):
        record=json.loads(path.read_text())
        for cid in record.get('case_ids',[]):
            if cid in found:continue
            case=next((row for row in data['cases'] if row['id']==cid),None)
            if case is None:continue
            filename=str(path.relative_to(root))
            try:check.verify_evidence(root,filename,case,historical=True)
            except (OSError,KeyError,TypeError,ValueError):continue
            found[cid]=filename
    for case in data['cases']:
        if case['id'] in found:
            case['retained_evidence']=sorted(set(case.get('retained_evidence',[]))|{found[case['id']]})
    matrix.write_text(yaml.safe_dump(data,allow_unicode=True,sort_keys=False))
    completed={case['id'] for case in data['cases'] if case.get('retained_evidence') or case['status']=='pass'}
    stages=sum(set(stage['cases'])<=completed for stage in data['stages'])
    print('ELASTICSEARCH_EVIDENCE_HISTORY completed_cases='+str(len(completed))+'/59 completed_stages='+str(stages)+'/18 current_accepted='+str(data['acceptance']['accepted'])+'/59',flush=True)


def recover_children(root,directory):
    """Read completed children independently of their parent's running/exit state."""
    if directory.resolve().parent != (root/'logs/elasticsearch-cases').resolve():
        raise ValueError('full evidence recovery requires its fixed local parent directory')
    match=re.fullmatch(r'only-\d{8}-\d{6}-([a-f0-9]{32})',directory.name)
    if not match or any(not (directory/name).is_file() for name in ('launch-inputs.json','launch-sources.tar.gz')):
        raise ValueError('full evidence recovery requires a bound parent')
    for name in ('exit','finalized'):
        if (directory/name).exists() and not re.fullmatch(r'\d+', (directory/name).read_text().strip()):
            raise ValueError('parent durable state is malformed')
    run_id=match[1]
    # This fixed host/path read does not acquire a lab, sync source, run a test,
    # or modify remote objects. Only regular child transcripts and exits move.
    script='''import sys,tarfile
from pathlib import Path
directory=Path('/var/tmp/kubeauto-elasticsearch-history')/('full-'+sys.argv[1])
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as archive:
    for path in sorted(directory.iterdir()):
        closed=all((directory/(path.stem+'.'+suffix)).is_file() for suffix in ('exit','finalized'))
        if closed and path.is_file() and not path.is_symlink() and path.suffix in ('.log','.exit','.finalized'):
            archive.add(path,arcname=path.name,recursive=False)
            if path.suffix=='.log':
                checkpoint=Path('/usr/local/kubeauto/logs/elasticsearch-cases')/('full-child-'+sys.argv[1]+'-'+path.stem)
                for name in ('launch-inputs.json','launch-provenance.json','launch-sources.tar.gz'):
                    source=checkpoint/name
                    if source.is_file() and not source.is_symlink():
                        archive.add(source,arcname='checkpoints/'+path.stem+'/'+name,recursive=False)
'''
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','root@192.168.122.2','python3','-',run_id],
        input=script.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    if result.returncode:raise ValueError('read-only full child evidence collection failed rc='+str(result.returncode))
    children=directory/'children';children.mkdir(mode=0o700,exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout),mode='r:gz') as archive:
        for member in archive.getmembers():
            name=Path(member.name)
            checkpoint=(len(name.parts)==3 and name.parts[0]=='checkpoints' and name.parts[1] in SCOPES
                        and name.name in ('launch-inputs.json','launch-provenance.json','launch-sources.tar.gz'))
            transcript=(name.name==member.name and name.suffix in ('.log','.exit','.finalized')
                        and name.stem in SCOPES.keys()|{'bootstrap','storage','dr-bootstrap'})
            if not member.isfile() or not (checkpoint or transcript) or member.size>128*1024**2:
                raise ValueError('child evidence archive exceeds its fixed boundary')
            path=children/name
            raw=archive.extractfile(member).read()
            if path.exists() and path.read_bytes()!=raw:raise ValueError('retained child evidence changed')
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            path.write_bytes(raw);path.chmod(0o600)
    inputs=json.loads((directory/'launch-inputs.json').read_text())
    # Verify the original Python/shell bytes against the frozen parent inputs.
    with tarfile.open(directory/'launch-sources.tar.gz','r:gz') as archive:
        for name,checksum in inputs.items():
            filename,_,fragment=name.partition('#')
            if Path(filename).suffix not in ('.py','.sh'):continue
            raw=archive.extractfile(filename).read()
            actual=(validator(root).python_dependency_digest(raw,fragment) if fragment else hashlib.sha256(raw).hexdigest())
            if actual!=checksum:raise ValueError('original parent source differs from its frozen identity')
    matrix=yaml.safe_load((root/'tests/elasticsearch-test-matrix.yaml').read_text())
    for module,allowed in SCOPES.items():
        log=children/(module+'.log');marker='ELASTICSEARCH_MODULE_PASS module='+module
        if not allowed or not log.is_file():continue
        fences={name:children/(module+'.'+name) for name in ('exit','finalized')}
        if any(not path.is_file() or path.read_text().strip()!='0' for path in fences.values()):continue
        lines=log.read_text().splitlines()
        if marker not in lines or any(re.match(r'^(?:ELASTICSEARCH_(?:FIRST_FAILURE|[A-Z_]*FAIL)\b|Traceback|\[FAIL\])',line) for line in lines):continue
        cases=[case for case in matrix['cases'] if case['id'] in allowed and case['success_marker'] in lines]
        if not cases:continue
        record={'historical_only':True,'case_ids':[case['id'] for case in cases],
            'durable_rc':0,'failure_markers':0,'product_entry':cases[0]['product_entry'],
            'scoped_cleanup':True,'scoped_cleanup_marker':marker,'dependencies':inputs,
            'raw_logs':[binding(root,log)],'durable':{name:binding(root,path) for name,path in fences.items()},
            'provenance':[binding(root,directory/name) for name in ('launch-inputs.json','launch-sources.tar.gz')]}
        path=root/'tests/evidence'/('elasticsearch-'+directory.name+'-child-'+module+'.json')
        raw=json.dumps(record,sort_keys=True,indent=2)+'\n'
        if path.exists():
            previous=json.loads(path.read_text())
            if {key:value for key,value in previous.items() if key!='provenance'} != {key:value for key,value in record.items() if key!='provenance'}:
                raise ValueError('historical child receipt identity already exists')
            for item in previous.get('provenance',[]):validator(root).bound_file(root,item)
        else:path.write_text(raw)
        checkpoint=children/'checkpoints'/module
        if not all((checkpoint/name).is_file() for name in ('launch-inputs.json','launch-provenance.json','launch-sources.tar.gz')):
            continue
        # Accept only the child's own frozen inputs, never substitute today's
        # dependency hashes or the failed parent's exit for this module.
        original=json.loads((checkpoint/'launch-inputs.json').read_text())
        check=validator(root)
        with tarfile.open(checkpoint/'launch-sources.tar.gz','r:gz') as archive:
            for name,checksum in original.items():
                filename,_,fragment=name.partition('#')
                if Path(filename).suffix not in ('.py','.sh'):continue
                source=archive.extractfile(filename).read()
                actual=check.python_dependency_digest(source,fragment) if fragment else hashlib.sha256(source).hexdigest()
                if actual!=checksum:raise ValueError('original child source differs from its frozen identity')
        current=dict(record)
        current.pop('historical_only')
        current['dependencies']=original
        current['provenance']=[binding(root,checkpoint/name) for name in ('launch-inputs.json','launch-provenance.json','launch-sources.tar.gz')]
        current_path=path.with_name(path.stem+'-scoped.json')
        current_raw=json.dumps(current,sort_keys=True,indent=2)+'\n'
        if current_path.exists() and current_path.read_text()!=current_raw:raise ValueError('scoped child receipt identity already exists')
        current_path.write_text(current_raw)
        for case in cases:
            try:check.verify_evidence(root,str(current_path.relative_to(root)),case)
            except (OSError,KeyError,TypeError,ValueError,tarfile.TarError):continue
            case.update(status='pass',evidence=[str(current_path.relative_to(root))])
            case.pop('pending_reason',None)
            print('ELASTICSEARCH_CHILD_EVIDENCE_ACCEPTED id='+case['id']+' module='+module,flush=True)
    matrix['acceptance']['accepted']=sum(case['status']=='pass' for case in matrix['cases'])
    matrix['meta']['result']='PASS' if matrix['acceptance']['accepted']==59 else 'PENDING'
    (root/'tests/elasticsearch-test-matrix.yaml').write_text(yaml.safe_dump(matrix,allow_unicode=True,sort_keys=False))
    retain_history(root)


def publish(root,directory,marker,scope):
    launch=json.loads((directory/'launch-inputs.json').read_text())
    if launch!=launch_dependencies(root,scope):raise ValueError('source changed after launch; retained evidence cannot sign current inputs')
    for name in ('exit','finalized'):
        if (directory/name).read_text().strip()!='0':raise ValueError('module durable exit is not successful')
    raw=directory/'raw.log';lines=raw.read_text().splitlines()
    if marker not in lines or any(re.match(r'^(?:ELASTICSEARCH_(?:FIRST_FAILURE|[A-Z_]*FAIL)\b|Traceback|\[FAIL\])',line) for line in lines):
        raise ValueError('module transcript lacks its clean success boundary')
    matrix=root/'tests/elasticsearch-test-matrix.yaml';data=yaml.safe_load(matrix.read_text())
    allowed=SCOPES.get(scope,set())
    cases=[case for case in data['cases'] if case['id'] in allowed and case['success_marker'] in lines]
    check=validator(root)
    for case in cases:check.require_case_markers(case,lines)
    if scope=='only' and ({case['id'] for case in cases}!=SCOPES['only'] or 'LAB_CLEAN_VERIFY_PASS' not in lines):
        raise ValueError('complete delivery requires every case and final native cleanup')
    if not cases:return []
    destination=root/'tests/evidence'/('elasticsearch-'+directory.name+'.json')
    record={'case_ids':[case['id'] for case in cases],'durable_rc':0,'failure_markers':0,
            'product_entry':cases[0]['product_entry'],'scoped_cleanup':True,'scoped_cleanup_marker':marker,
            'dependencies':launch,'raw_logs':[binding(root,raw)],
            'durable':{name:binding(root,directory/name) for name in ('exit','finalized')}}
    record['provenance']=[binding(root,directory/name) for name in ('launch-provenance.json','launch-sources.tar.gz')
                          if (directory/name).is_file()]
    scoped={}
    for case in cases:
        owner=check.CASE_MODULES[case['id']]
        if owner not in scoped:scoped[owner]=dependencies(root,owner)
    record['case_dependencies']={case['id']:dict(scoped[check.CASE_MODULES[case['id']]]) for case in cases}
    if destination.exists():raise ValueError('evidence identity already exists')
    documentation=destination.with_name(destination.stem+'-documentation.json')
    doc_record=None
    if any(case['id']=='ES-58' for case in cases):
        doc_record=dict(record,case_ids=['ES-58'],dependencies=dict(launch),
                        product_entry=next(case['product_entry'] for case in cases if case['id']=='ES-58'))
        for filename in ('operations-manual.md','technical-whitepaper.md','development-manual.md'):
            path=root/'docs/middleware/elasticsearch'/filename
            doc_record['dependencies'][str(path.relative_to(root))]=digest(path)
        path=root/'tests/evidence/elasticsearch-documentation.json'
        doc_record['dependencies'][str(path.relative_to(root))]=digest(path)
        doc_record['case_dependencies']={'ES-58':dict(doc_record['dependencies'])}
        if documentation.exists():raise ValueError('documentation evidence identity already exists')
    destination.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    try:
        if doc_record:documentation.write_text(json.dumps(doc_record,sort_keys=True,indent=2)+'\n')
        for case in cases:
            path=documentation if case['id']=='ES-58' else destination
            check.verify_evidence(root,str(path.relative_to(root)),case)
    except Exception:
        destination.unlink();documentation.unlink(missing_ok=True);raise
    for case in cases:
        path=documentation if case['id']=='ES-58' else destination
        case.update(status='pass',evidence=[str(path.relative_to(root))])
        case.pop('pending_reason',None)
    data['acceptance']['accepted']=sum(case['status']=='pass' for case in data['cases'])
    # Complete delivery is an explicit final gate, never inferred here.
    data['meta']['result']='PASS' if data['acceptance']['accepted']==59 else 'PENDING'
    matrix.write_text(yaml.safe_dump(data,allow_unicode=True,sort_keys=False))
    return record['case_ids']


def prepare_full(root):
    spec=importlib.util.spec_from_file_location('es_full_validator',root/'tests/helpers/validate_elasticsearch_matrix.py')
    validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
    data=yaml.safe_load((root/'tests/elasticsearch-test-matrix.yaml').read_text());records={}
    expected=SCOPES['only']-{'ES-59'}
    cases=[case for case in data['cases'] if case['id'] in expected]
    if len(cases)!=len(expected) or {case['id'] for case in cases}!=expected:
        raise ValueError('final launch requires all 58 prerequisite matrix items')
    missing=[case['id'] for case in cases if case['status']!='pass' or not case['evidence']]
    if missing:raise ValueError('final launch requires current PASS evidence: '+', '.join(missing))
    for case in cases:
        for filename in case['evidence']:
            validator.verify_evidence(root,filename,case);records[filename]=digest(root/filename)
    record={'case_ids':sorted(SCOPES['contract']|SCOPES['artifacts']),'evidence':records,'dependencies':dependencies(root)}
    path=root/'tests/evidence/elasticsearch-full-prerequisites.json'
    path.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    print('ELASTICSEARCH_FULL_PREREQUISITES_PASS sha256='+digest(path))


def require_current(root,scope):
    check=validator(root)
    data=yaml.safe_load((root/'tests/elasticsearch-test-matrix.yaml').read_text())
    cases=[case for case in data['cases'] if case['id'] in SCOPES[scope]]
    if (not cases or len(cases)!=len(SCOPES[scope]) or {case['id'] for case in cases}!=SCOPES[scope]
            or any(case['status']!='pass' or not case['evidence'] for case in cases)):
        raise ValueError('current prerequisite evidence is missing')
    for case in cases:
        for filename in case['evidence']:check.verify_evidence(root,filename,case)
    print('ELASTICSEARCH_CURRENT_EVIDENCE_REUSED scope='+scope+' cases='+str(len(cases)))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('freeze','publish','refresh','prepare-full','require-current','retain-history','recover-children'))
    parser.add_argument('directory',type=Path,nargs='?')
    parser.add_argument('--marker')
    parser.add_argument('--scope')
    args=parser.parse_args()
    if args.action=='retain-history':retain_history(ROOT);return
    if args.action=='recover-children':
        if args.directory is None:raise ValueError('bound full parent directory required')
        recover_children(ROOT,args.directory);return
    if args.action=='require-current':
        try:require_current(ROOT,args.scope)
        except (OSError,ValueError,KeyError,TypeError):raise SystemExit(1)
        return
    if args.action=='prepare-full':
        prepare_full(ROOT);return
    if args.action=='refresh':
        print('ELASTICSEARCH_EVIDENCE_INVALIDATED cases='+','.join(refresh(ROOT)))
        return
    if args.directory is None:raise ValueError('evidence directory required')
    if args.action=='freeze':
        freeze(ROOT,args.directory,args.scope)
    else:
        if not args.marker or not args.scope:raise ValueError('module cleanup/success marker and scope required')
        cases=publish(ROOT,args.directory,args.marker,args.scope)
        print('ELASTICSEARCH_EVIDENCE_ACCEPTED cases='+','.join(cases))


if __name__=='__main__':main()
