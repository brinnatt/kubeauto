#!/usr/bin/env python3
"""Only isolated owning CLI boundaries for the real full-shell contract."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[2]
WORK=BASE/'logs/longhorn-full/run.fixture'
CONF=json.loads((BASE/'fixture.json').read_text())
TRACE=BASE/'trace.jsonl'

def event(kind,**fields):
    # One append/write is atomic for these bounded short records.
    with TRACE.open('a') as stream:stream.write(json.dumps({'event':kind,**fields},sort_keys=True)+'\n')

def cluster(name):return BASE/'clusters'/name

def worker(branch):
    stage=os.environ['FIXTURE_STAGE'];directory=Path(os.environ['FIXTURE_PARALLEL'])
    owned=directory/(branch+'.owned');owned.write_text('original')
    try:
        event('parallel_start',stage=stage,branch=branch)
        (directory/(branch+'.started')).touch()
        other='v2' if branch=='v1' else 'v1'
        deadline=time.monotonic()+5
        while not (directory/(other+'.started')).exists():
            if time.monotonic()>deadline:raise RuntimeError('parallel branches did not overlap')
            time.sleep(.01)
        event('parallel_overlap',stage=stage,branch=branch)
        if CONF.get('fail')==stage+':'+branch:raise SystemExit(23)
    finally:
        owned.unlink();event('parallel_clean',stage=stage,branch=branch)

def own(mode):
    prerequisites={
        'v2-aio-install-qualification':('v2-resources-qualification',),
        'v2-dr-target-install-qualification':('v2-dr-target-core',),
        'v1-ownership-v2-linked-acceptance':('v2-aio-install-qualification','core-smoke','disk-qualification'),
        'v2-recurring-selector-qualification':('v2-backup-store-qualification',),
        'v2-dr-store-qualification':('v2-dr-target-install-qualification','v2-backup-store-qualification'),
        'v2-dr-standby-qualification':('v2-dr-store-qualification',),
        'v2-dr-activation-qualification':('v2-dr-standby-qualification',),
        'v1-backup-v2-data-protection':('v2-dr-activation-qualification','v2-recurring-selector-qualification'),
        'system-restore':('v1-backup-v2-data-protection',),
        'v2-upgrade-lifecycle':('v2-failure','v1-backup-v2-data-protection'),
        'v2-clone':('v2-upgrade-lifecycle','v1-ownership-v2-linked-acceptance'),
        'cleanup-qualification':('cleanup','v2-middleware'),
        'v2-cleanup-target':('cleanup-qualification',),
        'v2-disk-lifecycle':('v2-cleanup-target',),
        'v2-cleanup':('v2-disk-lifecycle',),
        'node-lifecycle':('v2-cleanup',),
        'uninstall':('node-lifecycle',),
    }
    for prerequisite in prerequisites.get(mode,()):
        assert (WORK/'accepted'/(prerequisite+'.json')).is_file(), 'missing accepted producer '+prerequisite
    if mode=='cleanup-qualification':
        assert cluster('longhorn-v2-gate').is_dir() and cluster('longhorn-v2-dr-target').is_dir()
    event('module_start',stage=mode)
    fixture=BASE/('owned-'+mode);fixture.write_text('own')
    rc=0
    try:
        if mode.startswith('v1-') and '-v2-' in mode:
            directory=BASE/('parallel-'+mode);directory.mkdir()
            def run(branch):
                prefix=directory/branch
                env=dict(os.environ,FIXTURE_STAGE=mode,FIXTURE_PARALLEL=str(directory))
                result=subprocess.run(['bash',str(BASE/'tests/helpers/run-durable-gate.sh'),str(prefix),
                    'FIXTURE_PARALLEL_EXIT',sys.executable,str(BASE/'tests/helpers/fixture-cli.py'),'worker',branch],
                    env=env,text=True,capture_output=True)
                (directory/(branch+'.log')).write_text(result.stdout+result.stderr)
                return result.returncode
            with ThreadPoolExecutor(2) as pool:codes=list(pool.map(run,('v1','v2')))
            event('parallel_complete',stage=mode,v1_rc=codes[0],v2_rc=codes[1])
            if any(codes):raise SystemExit(23)
        if CONF.get('fail')==mode:raise SystemExit(19)
        if mode=='v2-core':cluster('longhorn-v2-gate').mkdir(parents=True)
        elif mode=='v2-dr-target-core':cluster('longhorn-v2-dr-target').mkdir(parents=True)
        elif mode=='core':cluster('longhorn-gate').mkdir(parents=True)
        elif mode in ('v2-cleanup-target','v2-cleanup'):
            scope='target' if mode.endswith('target') else 'source'
            path=Path(CONF['proof_root'])/('longhorn-v2-'+scope+'-released-'+('192.168.47.149' if scope=='target' else '192.168.47.148')+'.json')
            path.write_text(json.dumps({'scope':scope,'host':'192.168.47.149' if scope=='target' else '192.168.47.148'}))
            shutil.rmtree(cluster('longhorn-v2-dr-target' if scope=='target' else 'longhorn-v2-gate'))
        elif mode in ('v2-disk-lifecycle','node-lifecycle'):
            scope='target' if mode=='v2-disk-lifecycle' else 'source'
            key='LONGHORN_V2_TARGET_RELEASE_PROOF' if scope=='target' else 'LONGHORN_V2_RELEASE_PROOF'
            proof=json.loads(Path(os.environ[key]).read_text())
            assert proof['scope']==scope
            event('release_consumed',stage=mode,**proof)
            if mode=='node-lifecycle':
                assert not cluster('longhorn-v2-gate').exists() and not cluster('longhorn-v2-dr-target').exists()
                target=json.loads(Path(os.environ['LONGHORN_V2_TARGET_RELEASE_PROOF']).read_text())
                assert target=={'scope':'target','host':'192.168.47.149'}
                event('release_consumed',stage=mode,**target)
        print('FIXTURE_OWNING_MODULE_PASS stage='+mode,flush=True)
    finally:
        fixture.unlink();event('module_clean',stage=mode)


def evidence(command,args):
    if command=='clone-accept':
        own('v2-clone');return
    if command=='ready':return
    if command=='freeze':event('frozen');return
    if command=='verify-preflight':return
    if command=='init':
        (WORK/'stages').mkdir();(WORK/'accepted').mkdir();event('initialized');return
    if command=='check-inputs':return
    if command=='release-proof':
        scope,host=args
        path=Path(CONF['proof_root'])/('longhorn-v2-'+scope+'-released-'+host+'.json')
        assert path.exists();print(path);return
    if command=='accept':
        mode=args[0];prefix=Path(args[args.index('--prefix')+1])
        assert Path(str(prefix)+'.exit').read_text().strip()=='0'
        assert Path(str(prefix)+'.finalized').read_text().strip()=='0'
        assert Path(str(prefix)+'.log').read_text().splitlines().count('LONGHORN_FULL_CHILD_EXIT rc=0')==1
        (WORK/'accepted'/(mode+'.json')).write_text(json.dumps({'stage':mode}))
        event('accepted',stage=mode);return
    if command=='finalize':
        actual={p.stem for p in (WORK/'accepted').iterdir()}
        assert actual==set(CONF['modes']) and len(actual)==len(CONF['modes'])
        assert not list(BASE.glob('owned-*'))
        (WORK/'full-result.json').write_text(json.dumps({'outer_pending':True}))
        event('children_complete')
        print('LONGHORN_DELIVERY_BRANCH_PASS');return
    raise ValueError('unexpected evidence fixture command')

kind=sys.argv[1];args=sys.argv[2:]
if kind=='evidence':evidence(args[0],args[1:])
elif kind=='own':own(args[0])
elif kind=='worker':worker(args[0])
elif kind=='context':
    if args[0]=='context-fingerprint':print('{}')
    else:
        record=Path(args[args.index('--record')+1]);assert json.loads(record.read_text())=={}
        event('context_restored')
elif kind=='lab':
    if args[0]=='archive-core':shutil.rmtree(cluster('longhorn-gate'));event('core_archived')
    elif args[0]=='verify-core':assert cluster('longhorn-gate').is_dir()
    else:raise ValueError('unexpected lab fixture command')
elif kind=='wipe':
    hosts=[x for x in args if not x.startswith('--')]
    allowed={'192.168.122.'+x for x in ('135','40','72','212','165','238')}|{'192.168.47.'+x for x in ('146','147','148','149')}
    assert set(hosts)<=allowed and hosts
    if '--verify' in args:print('LAB_CLEAN_VERIFY_PASS')
    event('wipe',verify='--verify' in args,hosts=hosts)
elif kind=='local':print('FIXTURE_LOCAL_PASS stage='+args[0]);event('local_module',stage=args[0])
else:raise ValueError('unexpected isolated fixture boundary')
