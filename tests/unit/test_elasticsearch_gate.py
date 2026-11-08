"""Prove ES fixture refusal, exact cleanup and product orchestration boundaries."""
import importlib.util
import copy
from contextlib import redirect_stderr, redirect_stdout
from contextlib import contextmanager
from contextlib import nullcontext
from io import StringIO
import sys
import json
import signal
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load('elasticsearch-filesystem.py')
MODULE.SIZE = 32 * 1024**3
MODULE.MOUNT = '/data/kubeauto-longhorn-elasticsearch-v1'
LAB = load('elasticsearch-lab.py')
ARTIFACTS = load('elasticsearch-artifacts.py')
BUSINESS = load('elasticsearch-business.py')
MATRIX = load('validate_elasticsearch_matrix.py')
S3 = load('elasticsearch-s3.py')
RALLY = load('elasticsearch-rally.py')
NETWORK = load('elasticsearch-network.py')
DR = load('elasticsearch-dr.py')
COEXISTENCE = load('elasticsearch-coexistence.py')
EVIDENCE = load('elasticsearch-evidence.py')
FULL = load('elasticsearch-full.py')
STOP = load('elasticsearch-stop.py')


class BoundaryStopContracts(unittest.TestCase):
    def test_stop_accepts_the_real_dr_bootstrap_terminal_and_rejects_incomplete_output(self):
        import contextlib
        import io
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            (source/'ownership.json').write_text('{"id":"source-owner"}')
            (source/'baseline.json').write_text('{"cluster_uid":"source-uid"}')
            target=root/'target';target.mkdir();cluster=root/'cluster';cluster.mkdir()
            lab=SimpleNamespace(STATE=target,DIRECTORY=cluster,NAME='elasticsearch-dr',
                atomic=lambda path,value:path.write_text(json.dumps(value)),
                product=Mock(),configuration=Mock(),baseline=Mock(),storage=Mock(),
                api=lambda:SimpleNamespace(get=lambda *args:{'metadata':{'uid':'target-uid'}},run=Mock()))
            output=io.StringIO()
            with patch.object(DR,'SOURCE',source),patch.object(DR,'admission',return_value={}),contextlib.redirect_stdout(output):
                DR.bootstrap(lab)
            lab.product.assert_any_call('setup','elasticsearch-dr','90',timeout=7200)
            lab.storage.assert_called_once_with()
            stem=root/'dr-bootstrap'
            for suffix in ('.exit','.finalized'):Path(str(stem)+suffix).write_text('0')
            stem.with_suffix('.log').write_text(output.getvalue())
            STOP.finish_module(stem)
            stem.with_suffix('.log').write_text('ELASTICSEARCH_MODULE_PASS module=dr-bootstrap\n')
            with self.assertRaises(ValueError):STOP.finish_module(stem)

    def test_stop_preserves_current_module_cleanup_and_real_parent_signal_exit(self):
        import os
        import shutil
        import time
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'product';helpers=base/'tests/helpers';helpers.mkdir(parents=True)
            durable=helpers/'run-durable-gate.sh'
            shutil.copyfile(ROOT/'tests/helpers/run-durable-gate.sh',durable)
            history=root/'history';run='a'*32;directory=history/('full-'+run);directory.mkdir(parents=True)
            stem=directory/'performance';prefix=root/'parent'
            child=helpers/'child.sh';child.write_text('sleep 0.5\necho scoped-clean\necho ELASTICSEARCH_MODULE_PASS module=performance\n')
            scheduler=helpers/'elasticsearch-full.py'
            scheduler.write_text('import subprocess\nfrom pathlib import Path\n'
                +f'log=open({str(stem.with_suffix(".log"))!r},"w")\n'
                +f'p=subprocess.Popen(["bash",{str(durable)!r},{str(stem)!r},"CHILD_EXIT","bash",{str(child)!r}],stdout=log)\n'
                +f'Path({str(root/"ready")!r}).touch()\n'
                +'p.wait()\n'+f'Path({str(root/"unwanted-next-module")!r}).touch()\n')
            gate=helpers/'gate.sh';gate.write_text('trap "touch '+str(root/'context-restored')+'" EXIT\n'+sys.executable+' '+str(scheduler)+'\n')
            process=subprocess.Popen(['bash',str(durable),str(prefix),'PARENT_EXIT','bash',str(gate),'only'],
                env=dict(os.environ,ELASTICSEARCH_RUN_ID=run),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            try:
                deadline=time.monotonic()+5
                while not (root/'ready').exists() or not Path(str(stem)+'.pid').exists():
                    if process.poll() is not None or time.monotonic()>deadline:raise AssertionError('fixture did not start')
                    time.sleep(.01)
                with patch.object(STOP,'BASE',base),patch.object(STOP,'PREFIX',prefix),patch.object(STOP,'HISTORY',history):
                    STOP.stop()
                self.assertEqual(143,process.wait(timeout=10))
                self.assertEqual('0',Path(str(stem)+'.exit').read_text().strip())
                self.assertEqual('0',Path(str(stem)+'.finalized').read_text().strip())
                self.assertEqual('143',Path(str(prefix)+'.exit').read_text().strip())
                self.assertEqual('143',Path(str(prefix)+'.finalized').read_text().strip())
                self.assertTrue((root/'context-restored').exists())
                self.assertFalse((root/'unwanted-next-module').exists())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid,signal.SIGCONT);os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)

    def test_failed_or_marker_only_module_cannot_be_accepted_as_a_safe_success_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            stem=Path(temporary)/'performance'
            for rc,raw in (('1','ELASTICSEARCH_MODULE_PASS module=performance\n'),
                           ('0','ELASTICSEARCH_MODULE_PASS module=performance\nELASTICSEARCH_FIRST_FAILURE module=soak\n'),
                           ('0','scoped-clean-only\n')):
                for suffix in ('.exit','.finalized'):Path(str(stem)+suffix).write_text(rc)
                stem.with_suffix('.log').write_text(raw)
                with self.subTest(rc=rc,raw=raw),self.assertRaises(ValueError):STOP.finish_module(stem)


class BootstrapEnvironmentRecoveryContracts(unittest.TestCase):
    def test_inventory_checks_all_source_hosts_and_continues_after_stale_default_context(self):
        runner = ROOT/'tests/helpers/elasticsearch-runner.sh'
        script = r'''
source "$1"
ssh_elasticsearch() { bash -c "$1"; }
ssh() { bash -c "${!#}"; }
# A stale default kubeconfig must not hide the remaining hosts.
test() {
  if [[ "$*" == "-s /root/.kube/config" ]]; then return 0; fi
  if [[ "$*" == "-s "* || "$*" == "-f "* ]]; then return 1; fi
  builtin test "$@"
}
kubectl() { echo STALE_DEFAULT_CONTEXT >&2; return 1; }
systemctl() { echo "DropInPaths=/etc/systemd/system/kube-apiserver.service.d/90-stale.conf"; }
curl() { echo registry_http=200; }
uname() { :; }; getconf() { :; }; df() { :; }; cat() { :; }
findmnt() { :; }; ip() { :; }; find() { :; }; getent() { :; }; lslocks() { :; }
docker() { :; }
export -f ssh test kubectl systemctl curl uname getconf df cat findmnt ip find getent lslocks docker
elasticsearch_inventory
'''
        result = subprocess.run(['bash', '-eu', '-c', script, 'inventory-test', str(runner)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertEqual({'192.168.122.2', *LAB.HOSTS},
                         {line.removeprefix('HOST ') for line in result.stdout.splitlines() if line.startswith('HOST ')})
        self.assertEqual(8, result.stdout.count('registry_http=200'))
        self.assertIn('DropInPaths=', result.stdout)
        self.assertEqual(8, result.stderr.count('STALE_DEFAULT_CONTEXT')//3)
        self.assertIn('ELASTICSEARCH_POOL_INVENTORY_END read_only=true lease=false', result.stdout)

    def test_registry_restart_preserves_data_and_aio_and_refuses_other_containers(self):
        for fault in ('none', 'foreign-container', 'truncated-catalog', 'changed-registry', 'changed-aio'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);state=root/'state';state.mkdir()
                context=root/'clusters/aio/kubectl.kubeconfig';context.parent.mkdir(parents=True);context.write_text('private')
                data=root/'data';data.mkdir();bridge=root/'docker0';restarted=[]
                row={'Id':'registry-uid','Image':'image-sha','Config':{'Image':'brinnatt/registry:2'},
                     'Mounts':[{'Destination':'/var/lib/registry','Type':'bind','Source':'/data/registry'}]}
                def execute(argv,**kwargs):
                    if argv[:2]==['docker','ps']:
                        return 'local_registry\nforeign\n' if fault=='foreign-container' else 'local_registry\n'
                    if argv[:2]==['docker','inspect']:
                        value=copy.deepcopy(row)
                        if restarted and fault=='changed-registry':value['Id']='replacement'
                        return json.dumps([value])
                    if argv[:2]==['docker','exec']:
                        from urllib.parse import parse_qs, urlsplit
                        # Registry 2 rejects n above its default 1000-result maximum.
                        if int(parse_qs(urlsplit(argv[-1]).query)['n'][0])>1000:
                            raise ValueError('HTTP 400 pagination number invalid')
                        return json.dumps({'repositories':['preserved']*(1000 if fault=='truncated-catalog' else 1)})
                    self.assertEqual(['systemctl','restart','docker'],argv)
                    restarted.append(True);bridge.touch();return ''
                def get(kind,name=''):
                    if kind=='namespace':return {'metadata':{'uid':'replacement' if restarted and fault=='changed-aio' else 'aio-uid'}}
                    return {'items':[{'metadata':{'uid':'node-uid'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}]}
                def path(value):
                    return {'/data/registry':data,'/sys/class/net/docker0':bridge}.get(str(value),Path(value))
                response=Mock();response.__enter__=Mock(return_value=SimpleNamespace(status=200));response.__exit__=Mock(return_value=False)
                with (patch.object(LAB,'BASE',root),patch.object(LAB,'STATE',state),patch.object(LAB,'Path',side_effect=path),
                      patch.object(LAB,'execute',side_effect=execute),patch.object(LAB,'urlopen',return_value=response),
                      patch.object(LAB,'load_es',return_value=SimpleNamespace(API=lambda _,**kwargs:SimpleNamespace(get=get)))):
                    if fault=='none':
                        LAB.registry_recover()
                        self.assertEqual('complete',json.loads((state/'registry-recovery.json').read_text())['phase'])
                    else:
                        with self.assertRaises(ValueError):LAB.registry_recover()
                    self.assertEqual(0 if fault in ('foreign-container','truncated-catalog') else 1,len(restarted))
                    self.assertEqual('private',context.read_text())

    def test_empty_bootstrap_resume_checks_ownership_hardware_config_and_clean_exit_before_install(self):
        import yaml
        for fault in ('none','hardware','config','inventory','existing-context','dirty-host'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);state=root/'state';state.mkdir();directory=root/'cluster';directory.mkdir()
                owner={'phase':'admitted-empty-source','hosts':list(LAB.HOSTS),
                       'hardware':{h:{'product_uuid':h} for h in LAB.HOSTS}}
                (state/'ownership.json').write_text(json.dumps(owner))
                hosts=list(LAB.HOSTS[:3])*2+list(LAB.HOSTS[3:])
                if fault=='inventory':hosts[-1]=hosts[0]
                (directory/'hosts').write_text(''.join(h+' node\n' for h in hosts))
                config=dict(K8S_VER='1.33.6',REGISTRY_HOST_IP='192.168.122.2',KUBE_RESERVED_ENABLED='no',
                            SYS_RESERVED_ENABLED='no',SYS_RESERVED_ENFORCE='no',local_path_provisioner_install='no')
                if fault=='config':config['K8S_VER']='1.37.1'
                (directory/'config.yml').write_text(yaml.safe_dump(config))
                if fault=='existing-context':(directory/'kubectl.kubeconfig').touch()
                events=[]
                def execute(argv,**kwargs):
                    self.assertIn('--verify',argv);events.append('clean-verified')
                    if fault=='dirty-host':raise ValueError('host is not clean')
                with (patch.object(LAB,'STATE',state),patch.object(LAB,'DIRECTORY',directory),
                      patch.object(LAB,'host_capacity',side_effect=lambda h:{'product_uuid':'foreign' if fault=='hardware' else h}),
                      patch.object(LAB,'execute',side_effect=execute),
                      patch.object(LAB,'bootstrap_platform',side_effect=lambda:events.append('product-install')) as install):
                    if fault=='none':
                        LAB.bootstrap_resume();self.assertEqual(['clean-verified','product-install'],events)
                    else:
                        with self.assertRaises(ValueError):LAB.bootstrap_resume()
                        install.assert_not_called()


class FullCompositionContracts(unittest.TestCase):
    def test_stages_derive_from_all_owning_cases_and_child_markers_cannot_sign_other_groups(self):
        import yaml
        matrix=yaml.safe_load((ROOT/'tests/elasticsearch-test-matrix.yaml').read_text())
        cases={f'ES-{i:02}' for i in range(1,7)}
        self.assertEqual(2,FULL.completed_stages(matrix,cases))
        cases|={'ES-07','ES-09'};self.assertEqual(2,FULL.completed_stages(matrix,cases))
        cases|={'ES-08','ES-10','ES-11','ES-12'};self.assertEqual(4,FULL.completed_stages(matrix,cases))
        self.assertEqual(18,FULL.completed_stages(matrix,EVIDENCE.SCOPES['only']))
        raw='ELASTICSEARCH_CASE_PASS id=ES-09\nELASTICSEARCH_CASE_PASS id=ES-15\nELASTICSEARCH_MODULE_PASS module=business\n'
        self.assertEqual({'ES-15'},FULL.module_cases(raw,'business',EVIDENCE.SCOPES,matrix))
        for invalid in (raw+'ELASTICSEARCH_FIRST_FAILURE module=business rc=1\n',raw.replace('ELASTICSEARCH_MODULE_PASS','not-a-boundary')):
            with self.assertRaises(ValueError):FULL.module_cases(invalid,'business',EVIDENCE.SCOPES,matrix)

    def test_documentation_receipt_requires_current_files_and_explicit_author_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'tests/evidence').mkdir(parents=True);(root/'docs/middleware/elasticsearch').mkdir(parents=True)
            files={}
            for name in ('operations-manual.md','technical-whitepaper.md','development-manual.md'):
                path=root/'docs/middleware/elasticsearch'/name;path.write_text('reviewed fixture');files[str(path.relative_to(root))]=EVIDENCE.digest(path)
            receipt={'status':'pass','blocking_defects':[],'files':files,'versions':{'elasticsearch':'9.5.6','eck':'3.5.0','kubernetes':'1.33.6'},
                     'review':dict.fromkeys(('sources','content','novice_path','operator_path','diagrams'),'pass')}
            path=root/'tests/evidence/elasticsearch-documentation.json';path.write_text(json.dumps(receipt))
            FULL.documentation(root)
            (root/next(iter(files))).write_text('changed after review')
            with self.assertRaisesRegex(ValueError,'documentation changed'):FULL.documentation(root)
            receipt['review']['novice_path']='pending';path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError,'review is incomplete'):FULL.documentation(root)

    def test_documentation_has_an_independent_read_only_fixed_entry_before_final_regression(self):
        import os,shlex,shutil
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);helpers=root/'tests/helpers';helpers.mkdir(parents=True)
            shutil.copy(ROOT/'tests/helpers/elasticsearch-full.py',helpers)
            shutil.copytree(ROOT/'docs/middleware/elasticsearch',root/'docs/middleware/elasticsearch')
            evidence=root/'tests/evidence';evidence.mkdir();shutil.copy(ROOT/'tests/evidence/elasticsearch-documentation.json',evidence)
            binary=root/'.venv/bin/python';binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');binary.chmod(0o700)
            script='set -eu\nROOT="$1"\nsource "$2"\nelasticsearch_launch() { echo forbidden-live-launch; return 9; }\nelasticsearch_local_gate() { test "$1" = documentation; shift 2; "$@"; }\nelasticsearch_dispatch --elasticsearch-documentation\n'
            result=subprocess.run(['bash','-c',script,'contract',str(root),str(ROOT/'tests/helpers/elasticsearch-runner.sh')],
                env=dict(os.environ),capture_output=True,text=True,timeout=10)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertIn('ELASTICSEARCH_CASE_PASS id=ES-58',result.stdout)
            self.assertIn('ELASTICSEARCH_DOCUMENTATION_SCOPED_CLEAN_PASS',result.stdout)
            self.assertNotIn('forbidden-live-launch',result.stdout)
            self.assertNotIn('ELASTICSEARCH_CASE_PASS id=ES-59',result.stdout)

    def test_real_first_bootstrap_refusal_stops_composition_before_next_module(self):
        from contextlib import redirect_stdout
        import os
        import shutil
        fixture=DurableModuleContracts(methodName='test_real_bootstrap_rejects_changed_cluster_before_any_owned_mutation')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        root=fixture.root
        fixture.responses['namespace']['metadata']['uid']='foreign-context'
        (root/'responses.json').write_text(json.dumps(fixture.responses))
        shutil.copy(fixture.gate,root/'tests/helpers/elasticsearch-gate.sh')
        shutil.copy(ROOT/'tests/helpers/run-durable-gate.sh',root/'tests/helpers/run-durable-gate.sh')
        shutil.copy(ROOT/'tests/elasticsearch-test-matrix.yaml',root/'tests/elasticsearch-test-matrix.yaml')
        (root/'tests/evidence').mkdir();proof=root/'tests/evidence/static.json';proof.write_text('{}')
        dependencies={'fixture':'bound'}
        (root/'tests/evidence/elasticsearch-full-prerequisites.json').write_text(json.dumps({
            'dependencies':dependencies,'evidence':{'tests/evidence/static.json':EVIDENCE.digest(proof)},
            'case_ids':sorted(EVIDENCE.SCOPES['contract']|EVIDENCE.SCOPES['artifacts'])}))
        state=root/'state';lab=SimpleNamespace(STATE=state,DIRECTORY=root/'clusters/elasticsearch-gate',stream_command=LAB.stream_command,atomic=LAB.atomic)
        evidence=SimpleNamespace(dependencies=lambda _:dependencies,SCOPES=EVIDENCE.SCOPES)
        output=StringIO()
        with (redirect_stdout(output),redirect_stderr(StringIO()),patch.object(FULL,'BASE',root),patch.object(FULL,'load',side_effect=[lab,evidence]),patch.object(FULL,'documentation'),
              patch.dict(os.environ,ELASTICSEARCH_PARENT_LEASE_HELD='yes',ELASTICSEARCH_RUN_ID='a'*32)):
            with self.assertRaisesRegex(ValueError,'full child failed'):FULL.main()
        run=root/'kubeauto-elasticsearch-history'/('full-'+'a'*32)
        self.assertEqual('1',(run/'bootstrap.exit').read_text().strip())
        self.assertIn('changed since read-only inventory',(run/'bootstrap.log').read_text())
        self.assertFalse((run/'storage.log').exists());self.assertFalse((state/'ownership.json').exists())


class ParallelPlatformContracts(unittest.TestCase):
    def test_disjoint_hosts_and_parent_context_are_required_before_launch(self):
        environment={'ELASTICSEARCH_RUN_ID':'a'*32,'ELASTICSEARCH_CONTEXT_OWNER_RUN_ID':'a'*32}
        lab=SimpleNamespace(HOSTS=('source',),baseline=Mock())
        for hosts,env in ((('source',),environment),(('target',),{})):
            with (self.subTest(hosts=hosts,env=env),patch.object(FULL,'load',return_value=SimpleNamespace(HOSTS=hosts)),
                  patch.object(FULL,'run_module') as launch):
                with self.assertRaises(ValueError):FULL.platform_parallel(lab,EVIDENCE,Path('/unused'),env,('csi-probe',),Mock())
                launch.assert_not_called()

    def test_real_processes_overlap_and_independent_success_survives_either_failure(self):
        import os
        import shutil
        for failure in ('none','source','target'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);helpers=root/'tests/helpers';helpers.mkdir(parents=True)
                shutil.copy(ROOT/'tests/helpers/run-durable-gate.sh',helpers/'run-durable-gate.sh')
                shutil.copy(ROOT/'tests/elasticsearch-test-matrix.yaml',root/'tests/elasticsearch-test-matrix.yaml')
                python=root/'.venv/bin/python';python.parent.mkdir(parents=True);python.symlink_to(sys.executable)
                (helpers/'elasticsearch-dr.py').write_text('''import os,sys,time
from pathlib import Path
root=Path(os.environ['FIXTURE_ROOT'])
assert sys.argv[1:]==['bootstrap']
assert os.environ['KUBECONFIG']==str(root/'clusters/elasticsearch-dr/kubectl.kubeconfig')
assert (root/'baseline').read_text()=='source-uid'
(root/'target-started').touch()
for attempt in range(100):
    if (root/'source-started').exists():break
    time.sleep(0.01)
assert (root/'source-started').exists()
assert os.environ['FIXTURE_FAILURE']!='target'
(root/'target-complete').write_text('target-uid')
print('ELASTICSEARCH_DR_PLATFORM_PASS independent_kubernetes=true',flush=True)
''')
                gate=helpers/'elasticsearch-gate.sh'
                gate.write_text('''#!/usr/bin/env bash
set -eu
test "$ELASTICSEARCH_CONTEXT_OWNER_RUN_ID" = "$ELASTICSEARCH_RUN_ID"
test "$(cat "$FIXTURE_ROOT/baseline")" = source-uid
test "$KUBECONFIG" = "$FIXTURE_ROOT/clusters/elasticsearch-gate/kubectl.kubeconfig"
case "$1" in
  csi-probe)
    touch "$FIXTURE_ROOT/source-started"
    for attempt in {1..100}; do
      if test -f "$FIXTURE_ROOT/target-started"; then break; fi
      sleep 0.01
    done
    test -f "$FIXTURE_ROOT/target-started"
    test "$FIXTURE_FAILURE" != source
    printf source-uid > "$FIXTURE_ROOT/source-complete"
    echo 'ELASTICSEARCH_CASE_PASS id=ES-09'
    ;;
  standard) test -f "$FIXTURE_ROOT/source-complete" ;;
  *) exit 2 ;;
esac
printf 'ELASTICSEARCH_MODULE_PASS module=%s\\n' "$1"
''')
                (root/'baseline').write_text('source-uid')
                environment=dict(os.environ,ELASTICSEARCH_RUN_ID='a'*32,ELASTICSEARCH_CONTEXT_OWNER_RUN_ID='a'*32,
                                 FIXTURE_ROOT=str(root),FIXTURE_FAILURE=failure)
                lab=SimpleNamespace(HOSTS=('source',),baseline=lambda:self.assertEqual('source-uid',(root/'baseline').read_text()),
                                    stream_command=LAB.stream_command)
                directory=root/'run';directory.mkdir();accepted={}
                with (patch.object(FULL,'BASE',root),patch.object(FULL,'load',return_value=SimpleNamespace(HOSTS=('target',))),
                      patch.object(EVIDENCE,'freeze') as freeze,
                      patch.object(EVIDENCE,'publish',return_value=['ES-09']) as publish,
                      redirect_stderr(StringIO()),redirect_stdout(StringIO())):
                    if failure=='none':
                        FULL.platform_parallel(lab,EVIDENCE,directory,environment,('csi-probe','standard'),lambda name,cases:accepted.update({name:cases}))
                    else:
                        with self.assertRaisesRegex(ValueError,'full child failed'):
                            FULL.platform_parallel(lab,EVIDENCE,directory,environment,('csi-probe','standard'),lambda name,cases:accepted.update({name:cases}))
                self.assertEqual(['csi-probe'] if failure!='source' else [],[call.args[3] for call in publish.call_args_list])
                for call in publish.call_args_list:
                    checkpoint=call.args[1]
                    self.assertEqual('0',(checkpoint/'exit').read_text().strip())
                    self.assertEqual('0',(checkpoint/'finalized').read_text().strip())
                    self.assertIn('ELASTICSEARCH_MODULE_PASS module=csi-probe',(checkpoint/'raw.log').read_text())
                self.assertEqual('source-uid',(root/'baseline').read_text())
                self.assertEqual(failure!='source',(root/'source-complete').exists())
                self.assertEqual(failure!='target',(root/'target-complete').exists())
                self.assertEqual(failure!='source',(directory/'standard.log').exists())
                if failure!='target':
                    self.assertIn('dr-bootstrap',accepted)
                    self.assertEqual('0',(directory/'dr-bootstrap.exit').read_text().strip())
                    self.assertEqual('0',(directory/'dr-bootstrap.finalized').read_text().strip())
                if failure!='source':self.assertEqual({'ES-09'},accepted['csi-probe'])


class ProgressAccountingContracts(unittest.TestCase):
    def test_counts_derive_from_cases_and_only_complete_stages_advance(self):
        import yaml
        matrix=yaml.safe_load((ROOT/'tests/elasticsearch-test-matrix.yaml').read_text())
        for case in matrix['cases']:case['status']='pending' if case['id']=='ES-59' else 'pass'
        with tempfile.TemporaryDirectory() as temporary,redirect_stdout(StringIO()) as output:
            run_id='a'*32;directory=Path(temporary)/('full-'+run_id)
            cases={'ES-01'}
            FULL.report_progress(LAB,directory,matrix,cases,'contract',run_id)
            self.assertEqual(0,json.loads((directory/'progress.json').read_text())['accepted_stages'])
            cases|={'ES-02','ES-03','ES-04'}
            FULL.report_progress(LAB,directory,matrix,cases,'contract',run_id)
            before=(directory/'progress.json').read_bytes()
            FULL.report_progress(LAB,directory,matrix,cases,'contract',run_id)
            self.assertEqual(before,(directory/'progress.json').read_bytes())
            self.assertEqual(1,json.loads(before)['accepted_stages'])
            self.assertIn('run_id='+run_id+' scope=full matrix_cases=58/59 capability_stages=17/18 final_completed_steps=1/18',output.getvalue())

    def test_regression_mixed_runs_scope_or_totals_refuse_without_overwriting_progress(self):
        import yaml
        matrix=yaml.safe_load((ROOT/'tests/elasticsearch-test-matrix.yaml').read_text())
        for fault in ('lost-case','unknown-case','foreign-run','foreign-directory','scope','total','stage-count'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temporary,redirect_stdout(StringIO()):
                run_id='a'*32;directory=Path(temporary)/('full-'+run_id);cases={'ES-01','ES-02'}
                FULL.report_progress(LAB,directory,matrix,cases,'contract',run_id)
                path=directory/'progress.json'
                if fault=='lost-case':cases.remove('ES-02')
                elif fault=='unknown-case':cases.add('ES-60')
                elif fault=='foreign-directory':run_id='b'*32
                else:
                    state=json.loads(path.read_text())
                    key,value={'foreign-run':('run_id','b'*32),'scope':('scope','contract'),
                               'total':('applicable_cases',58),'stage-count':('accepted_stages',18)}[fault]
                    state[key]=value;path.write_text(json.dumps(state))
                before=path.read_bytes()
                with self.assertRaises(ValueError):FULL.report_progress(LAB,directory,matrix,cases,'contract',run_id)
                self.assertEqual(before,path.read_bytes())

    def test_real_child_publication_precedes_progress_and_survives_parent_failure(self):
        """Exercise the registration protocol; fixture output is not ES business evidence."""
        import os
        import shutil
        import yaml
        for failure in ('later-child','parent-interrupted','unregistered'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);helpers=root/'tests/helpers';helpers.mkdir(parents=True)
                (root/'tests/evidence').mkdir()
                shutil.copy(ROOT/'tests/helpers/validate_elasticsearch_matrix.py',helpers)
                durable=helpers/'run-durable-gate.sh';shutil.copy(ROOT/'tests/helpers/run-durable-gate.sh',durable)
                fixture=MatrixEvidenceContracts();fixture.setUp();self.addCleanup(fixture.doCleanups)
                matrix=root/'tests/elasticsearch-test-matrix.yaml';matrix.write_text(yaml.safe_dump(fixture.data))
                (root/'product.py').write_text('# pinned protocol fixture\n')
                (helpers/'elasticsearch-gate.sh').write_text('''set -eu
if test "$1" = business; then
  for id in 15 16 17 18 19 20; do echo "ELASTICSEARCH_CASE_PASS id=ES-$id"; done
  echo 'ELASTICSEARCH_MODULE_PASS module=business'
else
  echo 'ELASTICSEARCH_CASE_PASS id=ES-21'
  echo 'ELASTICSEARCH_FIRST_FAILURE module=lifecycle rc=7'
  exit 7
fi
''')
                driver=root/'driver.py'
                driver.write_text(f'''import importlib.util,json,os,sys
from pathlib import Path
def load(name):
    spec=importlib.util.spec_from_file_location(name,{str(ROOT/'tests/helpers')!r}+'/'+name+'.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
full=load('elasticsearch-full');evidence=load('elasticsearch-evidence');lab=load('elasticsearch-lab')
root=Path({str(root)!r});full.BASE=root
evidence.dependencies=lambda root,scope=None:{{'product.py':evidence.digest(root/'product.py')}}
if sys.argv[1]=='unregistered':evidence.publish=lambda *args:[]
run_id=os.environ['ELASTICSEARCH_RUN_ID'];directory=root/('full-'+run_id);directory.mkdir()
found=full.run_module(lab,evidence,directory,'business',dict(os.environ))
# The callback may count a child only after its real receipt and matrix are durable.
matrix=full.yaml.safe_load((root/'tests/elasticsearch-test-matrix.yaml').read_text())
assert matrix['acceptance']['accepted']==6
full.report_progress(lab,directory,matrix,found,'business',run_id)
if sys.argv[1]=='parent-interrupted':raise SystemExit(143)
full.run_module(lab,evidence,directory,'lifecycle',dict(os.environ))
''')
                run_id='a'*32;parent=root/'parent'
                result=subprocess.run(['bash',str(durable),str(parent),'PARENT_EXIT',sys.executable,str(driver),failure],
                    env=dict(os.environ,ELASTICSEARCH_RUN_ID=run_id),capture_output=True,text=True,timeout=20)
                expected_rc=143 if failure=='parent-interrupted' else 1
                self.assertEqual(expected_rc,result.returncode,result.stdout+result.stderr)
                for suffix in ('.exit','.finalized'):
                    self.assertEqual(str(expected_rc),Path(str(parent)+suffix).read_text().strip())
                current=yaml.safe_load(matrix.read_text());run=root/('full-'+run_id)
                if failure=='unregistered':
                    self.assertEqual(0,current['acceptance']['accepted'])
                    self.assertFalse((run/'progress.json').exists())
                    self.assertIn('child evidence did not accept its complete case set',result.stdout+result.stderr)
                    continue
                passed={case['id'] for case in current['cases'] if case['status']=='pass'}
                self.assertEqual(EVIDENCE.SCOPES['business'],passed)
                self.assertEqual(6,current['acceptance']['accepted'])
                self.assertEqual(passed,set(json.loads((run/'progress.json').read_text())['case_ids']))
                self.assertEqual([],MATRIX.validate(matrix))
                checkpoint=root/'logs/elasticsearch-cases'/('full-child-'+run_id+'-business')
                for name in ('exit','finalized'):self.assertEqual('0',(checkpoint/name).read_text().strip())
                if failure=='later-child':
                    self.assertEqual('7',(run/'lifecycle.exit').read_text().strip())
                    self.assertEqual('7',(run/'lifecycle.finalized').read_text().strip())
                    self.assertIn('ELASTICSEARCH_FIRST_FAILURE module=lifecycle rc=7',(run/'lifecycle.log').read_text())
                self.assertEqual('PENDING',current['meta']['result'])


class EvidencePublicationContracts(unittest.TestCase):
    def test_consumed_shared_helpers_storage_and_monitoring_are_frozen_and_changes_invalidate(self):
        import yaml
        inputs=EVIDENCE.dependencies(ROOT)
        shared=('tests/helpers/validate_elasticsearch_matrix.py','tests/helpers/longhorn-artifacts.py',
                'tests/helpers/longhorn-node-failure.py',
                'tests/helpers/lab-wipe-nodes.sh','tests/helpers/kafka-cleanup.sh',
                'roles/longhorn/files/longhorn.py','roles/longhorn/files/images.json',
                'roles/longhorn/files/longhorn-1.12.1.tgz',
                'roles/cluster-addon/tasks/prometheus.yml',
                'roles/cluster-addon/templates/prometheus/values.yaml.j2')
        for filename in shared:
            with self.subTest(dependency=filename):
                self.assertEqual(EVIDENCE.digest(ROOT/filename),inputs[filename])
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'tests/evidence').mkdir(parents=True)
            import shutil
            (root/'tests/helpers').mkdir()
            shutil.copy(ROOT/'tests/helpers/validate_elasticsearch_matrix.py',root/'tests/helpers')
            filename='roles/longhorn/files/longhorn.py';path=root/filename
            path.parent.mkdir(parents=True);path.write_bytes((ROOT/filename).read_bytes())
            record={'dependencies':{filename:inputs[filename]}}
            (root/'tests/evidence/source.json').write_text(json.dumps(record))
            matrix=root/'tests/elasticsearch-test-matrix.yaml'
            matrix.write_text(yaml.safe_dump({'meta':{'result':'PENDING'},'acceptance':{'accepted':1},
                'cases':[{'id':'ES-09','status':'pass','evidence':['tests/evidence/source.json']}]}))
            self.assertEqual([],EVIDENCE.refresh(root))
            path.write_text(path.read_text()+'\n# changed CSI prerequisite\n')
            self.assertEqual(['ES-09'],EVIDENCE.refresh(root))
            self.assertIn(filename,yaml.safe_load(matrix.read_text())['cases'][0]['pending_reason'])

    def test_only_owning_case_markers_with_unchanged_inputs_and_real_zero_fences_are_accepted(self):
        import shutil
        import yaml
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'tests/helpers').mkdir(parents=True);(root/'tests/evidence').mkdir()
            shutil.copy(ROOT/'tests/helpers/validate_elasticsearch_matrix.py',root/'tests/helpers')
            matrix=root/'tests/elasticsearch-test-matrix.yaml'
            data=yaml.safe_load((ROOT/'tests/elasticsearch-test-matrix.yaml').read_text())
            for case in data['cases']:
                case.update(status='pending',evidence=[])
                case.pop('retained_evidence',None)
            data['acceptance']['accepted']=0;data['meta']['result']='PENDING'
            matrix.write_text(yaml.safe_dump(data))
            product=root/'product.py';product.write_text('pinned source')
            inputs={'product.py':EVIDENCE.digest(product)}
            directory=root/'logs/contract-run';directory.mkdir(parents=True)
            (directory/'launch-inputs.json').write_text(json.dumps(inputs))
            (directory/'raw.log').write_text('ELASTICSEARCH_CASE_PASS id=ES-01\nELASTICSEARCH_CASE_PASS id=ES-09\nCLEAN\n')
            for name in ('exit','finalized'):(directory/name).write_text('0\n')
            with patch.object(EVIDENCE,'dependencies',return_value=inputs):
                self.assertEqual(['ES-01'],EVIDENCE.publish(root,directory,'CLEAN','contract'))
            current=yaml.safe_load(matrix.read_text())
            self.assertEqual(1,current['acceptance']['accepted']);self.assertEqual('pending',current['cases'][8]['status'])
            self.assertEqual([],MATRIX.validate(matrix))
            for defect in ('changed-source','failed-fence','failure-in-log'):
                case_dir=root/'logs'/defect;shutil.copytree(directory,case_dir)
                if defect=='failed-fence':(case_dir/'finalized').write_text('1')
                if defect=='failure-in-log':(case_dir/'raw.log').write_text((case_dir/'raw.log').read_text()+'ELASTICSEARCH_FIRST_FAILURE module=contract rc=1\n')
                dependencies={'product.py':'f'*64} if defect=='changed-source' else inputs
                with patch.object(EVIDENCE,'dependencies',return_value=dependencies),self.assertRaises(ValueError):
                    EVIDENCE.publish(root,case_dir,'CLEAN','contract')
            self.assertEqual(1,yaml.safe_load(matrix.read_text())['acceptance']['accepted'])
            self.assertEqual([],EVIDENCE.refresh(root))
            product.write_text('accepted dependency changed')
            self.assertEqual(['ES-01'],EVIDENCE.refresh(root))
            refreshed=yaml.safe_load(matrix.read_text())
            self.assertEqual(0,refreshed['acceptance']['accepted'])
            self.assertIn('product.py',refreshed['cases'][0]['pending_reason'])
            self.assertEqual([],MATRIX.validate(matrix))


class RunIsolationContracts(unittest.TestCase):
    def test_minio_compatibility_requires_live_settings_on_every_expected_member(self):
        key='repositories.blobstore.testkit.analyze.copy_during_write_contention'
        c={'elasticsearch_topology':'standard'}
        for settings in ([False,False,False],['false','false','false'],[False,False],
                         [False,False,True],[False,False,None]):
            with self.subTest(settings=settings):
                request=Mock(return_value={'nodes':{str(i):{'settings':{key:value}} for i,value in enumerate(settings)}})
                @contextmanager
                def connection(*args):yield request
                es=SimpleNamespace(connection=connection,member_count=lambda config:3)
                output=StringIO()
                with patch.object(LAB,'load_es',return_value=es),patch.object(LAB,'api'),redirect_stdout(output):
                    if settings in ([False,False,False],['false','false','false']):
                        LAB.verify_minio_analysis(c)
                        self.assertIn('ELASTICSEARCH_MINIO_ANALYSIS_COMPATIBILITY_PASS topology=standard',output.getvalue())
                    else:
                        with self.assertRaisesRegex(ValueError,'every ES member'):LAB.verify_minio_analysis(c)
                        self.assertNotIn('_PASS',output.getvalue())
                request.assert_called_once_with('GET','/_nodes/settings?flat_settings=true')

    def test_cleanup_retry_archives_post_teardown_diagnostics_and_refuses_unknown_state(self):
        for retained in ('first-failure-123', 'unknown-fixture', 'diagnostic-symlink'):
            with self.subTest(retained=retained), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);state=root/'state';state.mkdir()
                path=state/retained
                if retained=='diagnostic-symlink':
                    path=state/'first-failure-123';path.symlink_to(root, target_is_directory=True)
                else:
                    path.mkdir();(path/'nodes.json').write_text('original first failure')
                with (patch.object(LAB,'STATE',state),patch.object(LAB,'BASE',root),
                      patch.object(LAB,'DIRECTORY',root/'absent'),patch.object(LAB,'logging_cleanup'),
                      patch.object(LAB,'execute') as execute):
                    if retained=='first-failure-123':
                        LAB.cleanup_gate()
                        self.assertEqual([],list(state.iterdir()))
                        files=list((root/'kubeauto-elasticsearch-history').glob('diagnostics-*/state/first-failure-123/nodes.json'))
                        self.assertEqual(1,len(files));self.assertEqual('original first failure',files[0].read_text())
                        self.assertEqual(1,execute.call_count)
                        self.assertIn('--verify',execute.call_args.args[0])
                    else:
                        with self.assertRaisesRegex(ValueError,'unowned retained state'):LAB.cleanup_gate()
                        execute.assert_not_called();self.assertTrue(path.exists())

    def test_failure_diagnostics_do_not_repopulate_the_active_ownership_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary)/'state'
            with (patch.object(LAB,'STATE',state),patch.object(LAB,'diagnostics') as diagnostics,
                  patch.object(sys,'argv',['elasticsearch-lab.py','diagnostics']),
                  patch.dict('os.environ',{'ELASTICSEARCH_PARENT_LEASE_HELD':'yes'})):
                LAB.main()
            destination=diagnostics.call_args.args[0]
            self.assertTrue(destination.is_relative_to(state.parent/'kubeauto-elasticsearch-history'))
            self.assertEqual([],list(state.iterdir()))

    def test_backup_composition_stops_at_the_first_unmet_boundary(self):
        events=[]
        with (patch.object(LAB,'backup_source',side_effect=lambda:events.append('source')),
              patch.object(LAB,'backup_refusal',side_effect=ValueError('negative gate failed')),
              patch.object(LAB,'backup_restore') as restore,patch.object(LAB,'backup_features') as features):
            with self.assertRaisesRegex(ValueError,'negative gate failed'):LAB.backup()
            self.assertEqual(['source'],events);restore.assert_not_called();features.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)),patch.object(LAB,'baseline'),patch.object(LAB,'dr_fixture') as fixture:
            with self.assertRaisesRegex(ValueError,'source snapshot evidence'):LAB.backup_restore()
            fixture.assert_not_called()

    def test_retired_context_requires_same_directory_and_exact_admitted_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);directory=root/'clusters/k8s-dev';directory.mkdir(parents=True)
            context=directory/'kubectl.kubeconfig';context.write_text('admitted context')
            with patch.object(LAB,'STATE',root/'state'):
                identity=LAB.context_identity(directory)
                context.write_text('replaced context')
                with self.assertRaisesRegex(ValueError,'changed after admission'):LAB.retire_previous_context(directory,identity,'owner')
                self.assertTrue(context.exists())
                context.write_text('admitted context')
                LAB.retire_previous_context(directory,identity,'owner')
                self.assertFalse(directory.exists())
                self.assertEqual('admitted context',(root/'kubeauto-elasticsearch-history/owner/retired-k8s-dev/kubectl.kubeconfig').read_text())
                directory.mkdir()
                with self.assertRaisesRegex(ValueError,'unexpected previous'):LAB.retire_previous_context(directory,None,'next')

    def test_native_cleanup_archives_old_ledger_and_keeps_active_logging_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);state=root/'state';state.mkdir()
            (state/'ownership.json').write_text(json.dumps({'id':'old-owner','hosts':list(LAB.HOSTS)}))
            for name in ('ledger-standard.json','dr-disaster.json','logging-ownership.json'):(state/name).write_text(name)
            with patch.object(LAB,'STATE',state),patch.object(LAB,'DIRECTORY',root/'absent'),patch.object(LAB,'execute') as execute,patch.object(LAB,'s3_fixture'):
                execute.side_effect=ValueError('cleanup verification failed')
                with self.assertRaisesRegex(ValueError,'verification failed'):LAB.cleanup()
                self.assertTrue((state/'ownership.json').exists());self.assertTrue((state/'ledger-standard.json').exists())
                execute.side_effect=None
                LAB.cleanup()
            self.assertEqual(['logging-ownership.json'],[p.name for p in state.iterdir()])
            archive=root/'kubeauto-elasticsearch-history/old-owner/state'
            self.assertEqual('ledger-standard.json',(archive/'ledger-standard.json').read_text())
            self.assertTrue((archive/'ownership.json').exists())


class CSIProbeContracts(unittest.TestCase):
    def test_fsynced_payload_survives_relocation_and_growth_while_changed_binding_refuses(self):
        for replace_volume in (False,True):
            with self.subTest(replace_volume=replace_volume),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);state=root/'state';state.mkdir();data=root/'data';data.mkdir()
                receipts=root/'tests/evidence';receipts.mkdir(parents=True)
                (receipts/'elasticsearch-images-20261103.json').write_text(json.dumps({'images':[{
                    'key':'brinnatt/elasticsearch-client:9.5.1-kubeauto.1','talkedu_ref':'hub.talkedu.cn/kubeauto/elasticsearch-client:9.5.1-kubeauto.1','digest':'sha256:'+'a'*64}]}))
                resources={};commands=[];sizes=[];namespace=[None];expanded=[False]
                def apply(objects):
                    for obj in objects:
                        kind,name=obj['kind'].lower(),obj['metadata']['name'];obj=copy.deepcopy(obj)
                        obj['metadata'].update(uid=name+'-uid',resourceVersion='1')
                        if kind=='namespace':namespace[0]=name
                        if kind=='pod':
                            code=obj['spec']['containers'][0]['command'][2].replace('/probe',str(data)).replace('/tmp/ready',str(root/'ready')).replace('while True:time.sleep(10)','')
                            self.assertIn('socket.gethostbyname("kubernetes.default.svc")',code)
                            self.assertIn('socket.create_connection(("kubernetes.default.svc",443),timeout=10).close()',code)
                            code=code.replace('socket.gethostbyname("kubernetes.default.svc")','None').replace('socket.create_connection(("kubernetes.default.svc",443),timeout=10).close()','None')
                            result=subprocess.run([sys.executable,'-c',code],text=True,capture_output=True,timeout=10)
                            self.assertEqual(0,result.returncode,result.stderr)
                            self.assertIn('CSI_FSYNC_DATA_VERIFIED',result.stdout)
                            self.assertIn('@sha256:',obj['spec']['containers'][0]['image'])
                            self.assertEqual({'runAsNonRoot':True,'runAsUser':1000,'runAsGroup':1000,'fsGroup':1000},obj['spec']['securityContext'])
                            sizes.append(len((data/'data').read_bytes()))
                        resources[(kind,name)]=obj
                def get(kind,name='',namespace_arg=''):
                    if kind=='pv' and not name:return {'items':[]}
                    if kind=='pvc':return {'status':{'capacity':{'storage':'2Gi'}},'spec':{'accessModes':['ReadWriteOnce'],'resources':{'requests':{'storage':'2Gi'}}}}
                    if kind=='pv':return {'spec':{'capacity':{'storage':'2Gi'}}}
                    if kind=='volumes.longhorn.io':return {'spec':{'size':str(2*1024**3)}}
                    return resources.get((kind,name))
                def run(*args,**kwargs):
                    commands.append(args)
                    if 'logs' in args:return 'CSI_FSYNC_DATA_VERIFIED\n'
                    if 'exec' in args:
                        code=args[-1]
                        if 'hashlib' in code:return LAB.hashlib.sha256((data/'data').read_bytes()).hexdigest()+'\n'
                        return str(2*1024**3 if expanded[0] else 1024**3)+'\n'
                    if 'patch' in args:expanded[0]=True
                    if args[:2]==('delete','namespace'):resources.pop(('namespace',args[2]))
                    return ''
                binding={'pvc':'probe','pvc_uid':'pvc','pv':'pv','pv_uid':'pv','volume':'native','volume_uid':'native'}
                def volume(pod,**kwargs):
                    value=dict(binding)
                    if replace_volume and pod['metadata']['name']=='probe-1':value['volume_uid']='replacement'
                    return value
                api=SimpleNamespace(apply=apply,get=get,run=run)
                with (patch.object(LAB,'BASE',root),patch.object(LAB,'STATE',state),patch.object(LAB,'api',return_value=api),
                      patch.object(LAB,'baseline',return_value={'cluster_uid':'cluster'}),patch.object(LAB,'bind_namespace'),
                      patch.object(LAB,'volume_binding',side_effect=volume),patch.object(LAB,'load_es',return_value=SimpleNamespace(quantity=lambda v:2*1024**3))):
                    if replace_volume:
                        with self.assertRaisesRegex(ValueError,'replaced the original'):LAB.csi_probe()
                        self.assertNotIn(('delete','namespace',namespace[0],'--wait=true','--timeout=10m'),commands)
                        self.assertFalse(json.loads((state/'csi.json').read_text()).get('clean'))
                    else:
                        LAB.csi_probe()
                        record=json.loads((state/'csi.json').read_text())
                        self.assertTrue(record['clean']);self.assertEqual('complete',record['phase'])
                        self.assertNotEqual(record['first_node'],record['second_node'])
                        self.assertGreater(record['filesystem_after'],record['filesystem_before'])
                self.assertEqual([1024**2,1024**2],sizes)

    def test_scoped_cleanup_refuses_replaced_namespace_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)):
            (Path(temporary)/'csi.json').write_text(json.dumps({'namespace':'es-csi-owned','namespace_uid':'original'}))
            api=SimpleNamespace(get=lambda *args:{'metadata':{'uid':'foreign','labels':LAB.LABELS}},run=Mock())
            with patch.object(LAB,'api',return_value=api),self.assertRaisesRegex(ValueError,'changed namespace'):LAB.cleanup_csi()
            api.run.assert_not_called()


class CoexistenceContracts(unittest.TestCase):
    def test_recovery_write_requires_replicated_exact_data_and_cleans_only_bound_index(self):
        for fault in ('none','ack','document','count','replaced','existing'):
            with self.subTest(fault=fault):
                objects={};deleted=[];name='es-eck-recovery-probe'
                if fault=='existing':objects[name]={'uuid':'foreign','documents':{}}
                def request(method,path,body=None):
                    if '_settings' in path:
                        return {key:{'settings':{'index':{'uuid':value['uuid']}}} for key,value in objects.items()}
                    if path.endswith('/_refresh'):
                        if fault=='replaced':objects[name]['uuid']='foreign'
                        return {}
                    if '/_create/' in path:
                        objects[name]['documents']['probe']=body
                        return {'_shards':{'successful':1 if fault=='ack' else 2,'failed':0}}
                    if '/_doc/' in path:return {'_source':{} if fault=='document' else objects[name]['documents']['probe']}
                    if path.endswith('/_count'):return {'count':2 if fault=='count' else 1}
                    if method=='PUT':
                        self.assertEqual({'number_of_shards':1,'number_of_replicas':1},body['settings'])
                        objects[name]={'uuid':'owned','documents':{}};return {}
                    self.assertEqual(('DELETE','/'+name),(method,path))
                    deleted.append(objects.pop(name))
                with patch.object(COEXISTENCE.secrets,'token_hex',return_value='probe'):
                    if fault=='none':COEXISTENCE.confirm_write(request)
                    else:
                        with self.assertRaisesRegex(ValueError,'acknowledgement|document|identity|exists'):
                            COEXISTENCE.confirm_write(request)
                self.assertEqual(0 if fault in ('replaced','existing') else 1,len(deleted))
                if objects:self.assertEqual('foreign',objects[name]['uuid'])

    def test_retained_preparation_checks_ownership_capacity_and_restores_configuration_on_failure(self):
        class Prepared(Exception):pass
        for fault in ('none','foreign','overlap','unknown','setup'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);nodes=list(LAB.NODES)
                plan={'logging_nodes':nodes[:3],'upgrade_nodes':nodes[3:6]}
                if fault=='overlap':plan['upgrade_nodes'][0]=nodes[0]
                if fault=='unknown':plan['upgrade_nodes'][0]='foreign-node'
                (state/'coexistence-capacity.json').write_text(json.dumps(plan))
                (state/'namespaces.json').write_text(json.dumps({'logging':'owned'}))
                current={'selection':'original','logging_install':'no'};original=dict(current)
                def config(**updates):current.update(updates);return dict(current)
                def selected(topology,**updates):
                    config(selection=topology)
                    return {'elasticsearch_data_nodes':updates.get('elasticsearch_data_nodes',nodes[3:])}
                ns={'metadata':{'uid':'foreign' if fault=='foreign' else 'owned','labels':LAB.LABELS}}
                api=SimpleNamespace(get=lambda *args:ns)
                def product(*args,**kwargs):
                    self.assertEqual(('setup','cluster','07','--tags','elasticsearch'),args)
                    if fault=='setup':raise RuntimeError('setup failed')
                lab=SimpleNamespace(STATE=state,NODES=LAB.NODES,LABELS=LAB.LABELS,NAME='cluster',baseline=Mock(),
                    api=lambda:api,configuration=config,selected_config=Mock(side_effect=selected),
                    product=Mock(side_effect=product),verify_topology=Mock(),data_ledger=Mock(),
                    topology_state=Mock(side_effect=Prepared))
                with patch.object(COEXISTENCE,'capacity') as capacity,patch.object(COEXISTENCE,'fixtures') as fixtures:
                    if fault=='none':
                        with self.assertRaises(Prepared):COEXISTENCE.run(lab)
                        lab.verify_topology.assert_called_once_with('compact');lab.data_ledger.assert_called_once_with('compact')
                    else:
                        with self.assertRaisesRegex((ValueError,RuntimeError),'unbound|capacity plan|setup failed'):
                            COEXISTENCE.run(lab)
                        lab.topology_state.assert_not_called()
                    capacity.assert_not_called();fixtures.assert_not_called()
                if fault in ('foreign','overlap','unknown'):
                    lab.selected_config.assert_not_called();lab.product.assert_not_called()
                self.assertEqual(original,current)

    def test_foreign_service_refuses_through_real_preflight_and_only_owned_namespace_is_cleaned(self):
        from tests.unit.test_elasticsearch_delivery import configuration, es
        for interrupted in (False,True):
            with self.subTest(interrupted=interrupted):
                current=configuration('compact');original=copy.deepcopy(current);objects={};refused=[]
                def config(**updates):current.update(updates);return dict(current)
                def get(kind,name='',namespace=''):return copy.deepcopy(objects.get((kind,name,namespace)))
                def apply(items):
                    for obj in items:
                        obj=copy.deepcopy(obj);obj['metadata'].update(uid='fixture',resourceVersion='7')
                        objects[obj['kind'].lower(),obj['metadata']['name'],obj['metadata'].get('namespace','')]=obj
                def scenario(namespace):
                    apply([{'kind':'Namespace','metadata':{'name':namespace,'labels':dict(LAB.LABELS,**es.LABELS)}}])
                def run(*args,body=None,**kwargs):
                    if args[0]=='delete':
                        self.assertEqual('/api/v1/namespaces/es-isolation-owner',args[2])
                        self.assertEqual({'uid':'fixture','resourceVersion':'7'},json.loads(body)['preconditions'])
                        objects.clear()
                def refusal(reason,label):
                    with self.assertRaisesRegex(ValueError,reason):es.preflight(current,api)
                    refused.append(label)
                    if interrupted:raise RuntimeError('product interruption')
                api=SimpleNamespace(get=get,apply=apply,run=run)
                lab=SimpleNamespace(NAME='cluster',LABELS=LAB.LABELS,api=lambda:api,configuration=config,
                    selected_config=lambda _:dict(current),scenario_namespace=scenario,product_refusal=refusal,
                    verify_topology=Mock(),data_ledger=Mock())
                with patch.object(COEXISTENCE,'verify'),redirect_stdout(StringIO()) as output:
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError,'product interruption'):COEXISTENCE.ownership_refusal(lab,{})
                        self.assertNotIn('SUBCASE_PASS',output.getvalue())
                    else:
                        COEXISTENCE.ownership_refusal(lab,{})
                        self.assertIn('ES-53-ownership-refusal',output.getvalue())
                self.assertEqual(['foreign-resource'],refused);self.assertEqual({},objects);self.assertEqual(original,current)

    def test_repository_alias_and_completed_replay_use_real_operation_guards_before_writes(self):
        from tests.unit.test_elasticsearch_delivery import configuration, es
        from contextlib import nullcontext
        for interrupted in (False,True):
            with self.subTest(interrupted=interrupted),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);(state/'s3.json').write_text(json.dumps({
                    'bucket':'backups','endpoint':'https://s3.example.com'}))
                current=configuration('compact') | {'elasticsearch_s3_secret':'s3','logging_install':'no'}
                original=copy.deepcopy(current);repositories={};journals={};events=[];refused=[]
                def config(**updates):current.update(updates);return copy.deepcopy(current)
                def request(method,path,body=None):
                    events.append((method,path))
                    if path=='/':return {'cluster_uuid':'data-cluster'}
                    if path=='/_snapshot/_all':return dict(repositories)
                    name=path.split('/')[2].split('?')[0]
                    if method=='GET':return {name:copy.deepcopy(repositories[name])}
                    if method=='PUT' and path.endswith('?verify=false'):
                        repositories[name]=copy.deepcopy(body);return {'acknowledged':True}
                    if method=='DELETE':del repositories[name];return {'acknowledged':True}
                    raise AssertionError('product crossed the reserved backup boundary: '+path)
                def get(kind,name,namespace):return copy.deepcopy(journals.get(name))
                def apply(items):
                    for item in items:
                        item=copy.deepcopy(item);item['metadata'].update(uid='journal',resourceVersion='7')
                        journals[item['metadata']['name']]=item
                def run(*args,body=None):
                    self.assertEqual(('delete','--raw'),args[:2])
                    self.assertEqual({'uid':'journal','resourceVersion':'7'},json.loads(body)['preconditions'])
                    del journals[args[2].rsplit('/',1)[1]]
                api=SimpleNamespace(get=get,apply=apply,run=run)
                def refusal(reason,label):
                    es.validate(current)
                    if label=='alias-slm':
                        broken=copy.deepcopy(current)
                        broken['elasticsearch_requests'][0]['policy'].pop('retention')
                        with self.assertRaisesRegex(ValueError,'SLM policy needs'):es.validate(broken)
                    with patch.object(es,'connection',return_value=nullcontext(request)):
                        with self.assertRaisesRegex(ValueError,reason):es.operations(current,api)
                    refused.append(label)
                    if interrupted and len(refused)==4:raise RuntimeError('interrupted after replay refusal')
                lab=SimpleNamespace(STATE=state,api=lambda:api,load_es=lambda:es,configuration=config,
                    selected_config=lambda _:config(**original),product_refusal=refusal,
                    verify_topology=Mock(),data_ledger=Mock())
                with (patch.object(es,'connection',return_value=nullcontext(request)),
                      patch.object(COEXISTENCE,'verify'),redirect_stdout(StringIO()) as output):
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError,'interrupted after replay refusal'):COEXISTENCE.repository_refusals(lab,{})
                        self.assertNotIn('SUBCASE_PASS',output.getvalue())
                    else:
                        COEXISTENCE.repository_refusals(lab,{})
                        self.assertIn('ES-53-repository-refusal',output.getvalue())
                self.assertEqual(4,len(refused));self.assertEqual({},repositories);self.assertEqual({},journals)
                self.assertEqual(original,current)
                self.assertTrue(all(method=='GET' or path=='/' or path.endswith('?verify=false') or method=='DELETE'
                                    for method,path in events))
                if not interrupted:
                    (state/'coexistence.json').write_text('{}')
                    (state/'namespaces.json').write_text(json.dumps({'logging':'fixture'}))
                    previous_get=api.get
                    api.get=lambda kind,name='',namespace='': ({'metadata':{'uid':'fixture','labels':LAB.LABELS}}
                        if kind=='namespace' else previous_get(kind,name,namespace))
                    lab.baseline=Mock();lab.LABELS=LAB.LABELS
                    with (patch.object(es,'connection',return_value=nullcontext(request)),patch.object(COEXISTENCE,'verify'),
                          patch.object(COEXISTENCE,'shared_recovery',side_effect=RuntimeError('next boundary')),
                          patch.object(COEXISTENCE,'finish') as finish,redirect_stdout(StringIO()) as output):
                        with self.assertRaisesRegex(RuntimeError,'next boundary'):COEXISTENCE.isolation_repair(lab)
                        finish.assert_not_called();self.assertNotIn('ELASTICSEARCH_CASE_PASS',output.getvalue())
                    self.assertEqual(8,len(refused));self.assertEqual({},repositories);self.assertEqual({},journals)
                    self.assertEqual(original,current)

    def test_isolation_repair_refuses_foreign_or_faulted_baseline_before_any_scenario(self):
        for fault in ('namespace','owner','label','fault'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);(state/'coexistence.json').write_text('{}')
                (state/'namespaces.json').write_text(json.dumps({'logging':'owned'}))
                obj={'metadata':{'uid':'foreign' if fault=='owner' else 'owned',
                    'labels':{} if fault=='label' else LAB.LABELS}}
                if fault=='fault':(state/'fault-owned.json').write_text('{}')
                lab=SimpleNamespace(STATE=state,LABELS=LAB.LABELS,baseline=Mock(),configuration=Mock(return_value={}),
                    api=lambda:SimpleNamespace(get=lambda *args:None if fault=='namespace' else obj))
                with patch.object(COEXISTENCE,'repository_refusals') as scenario,patch.object(COEXISTENCE,'verify') as verify:
                    with self.assertRaisesRegex(ValueError,'bound fault-free'):COEXISTENCE.isolation_repair(lab)
                    scenario.assert_not_called();verify.assert_not_called()
                    self.assertEqual(1,lab.configuration.call_count)

    def test_business_reentry_preserves_claims_credentials_and_stops_on_failed_retirement(self):
        from tests.unit.test_elasticsearch_delivery import configuration,es
        for fault in ('none','claims','secret'):
            with self.subTest(fault=fault):
                configs={t:configuration(t) | {'elasticsearch_namespace':'es-'+t,'elasticsearch_requests':[],
                         'elasticsearch_s3_secret':'source-s3'}
                         for t in ('standard','compact')};current={};active={t:True for t in configs};calls=[]
                def config(**updates):current.update(updates);return copy.deepcopy(current)
                def selected(t,**updates):configs[t].update(updates);return config(**configs[t])
                def get(kind,name='',namespace=''):
                    t=namespace.removeprefix('es-')
                    if kind=='pvc':return {'items':[] if fault=='claims' and not active[t] else [{'metadata':{'name':'data','uid':t}}]}
                    return {'metadata':{'uid':'foreign' if fault=='secret' and not active[t] else 'credential'},'data':{'key':'preserved'}}
                def healthy(t):
                    selected(t)
                    if not active[t]:raise ValueError('business is still absent')
                def product(*args,**kwargs):
                    self.assertEqual(('setup','cluster','07','--tags','elasticsearch'),args)
                    es.validate(current);t=current['elasticsearch_namespace'].removeprefix('es-')
                    active[t]=current['elasticsearch_state']=='present';calls.append((t,active[t]))
                lab=SimpleNamespace(NAME='cluster',api=lambda:SimpleNamespace(get=get),selected_config=selected,
                    verify_topology=healthy,data_ledger=healthy,product=product,
                    topology_state=lambda t:{'pvc_uids':{'data':t}})
                with patch.object(COEXISTENCE,'verify') as peer,redirect_stdout(StringIO()) as output:
                    if fault=='none':
                        COEXISTENCE.business_reentry(lab,{})
                        self.assertEqual([('standard',False),('standard',True),('compact',False),('compact',True)],calls)
                        self.assertEqual(4,peer.call_count);self.assertIn('ES-53-efk-then-es',output.getvalue())
                    else:
                        with self.assertRaisesRegex(ValueError,'uninstall removed'):COEXISTENCE.business_reentry(lab,{})
                        self.assertEqual([('standard',False)],calls);peer.assert_not_called()
                        self.assertNotIn('SUBCASE_PASS',output.getvalue())

    def test_capacity_admits_disjoint_fixture_nodes_and_refuses_shortage_before_mutation(self):
        from tests.unit.test_elasticsearch_delivery import es
        for fault in ('none','memory','init-container'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                free={'master-a':12,'master-b':9,'master-c':7.8,'data-a':7,'data-b':7,'data-c':10,'data-d':7}
                if fault=='memory':free['master-b']=7
                nodes=[{'metadata':{'name':name},'status':{'allocatable':{'memory':'16Gi'}}} for name in free]
                pods=[{'spec':{'nodeName':name,'containers':[{'resources':{'requests':{'memory':str(16-value)+'Gi'}}}]}}
                      for name,value in free.items()]
                pods.append({'spec':{'nodeName':'master-a','containers':[{'resources':{'requests':{'memory':'99Gi'}}}]},
                             'status':{'phase':'Succeeded'}})
                if fault=='init-container':
                    pods[1]['spec']['initContainers']=[{'resources':{'requests':{'memory':'16Gi'}}}]
                api=SimpleNamespace(get=lambda kind:{'items':nodes if kind=='nodes' else pods},apply=Mock(),run=Mock())
                current={'selection':'original'}
                def config(**updates):current.update(updates);return dict(current)
                def selected(_):
                    config(selection='compact')
                    return {'elasticsearch_data_nodes':['data-a','data-b','data-c','data-d'],'elasticsearch_data_memory':'4Gi'}
                lab=SimpleNamespace(STATE=Path(temporary),NODES=tuple(free),api=lambda:api,load_es=lambda:es,
                    configuration=config,selected_config=selected,atomic=Mock())
                if fault=='none':
                    COEXISTENCE.capacity(lab)
                    self.assertEqual({'logging_nodes':['master-a','data-c','master-b'],
                        'upgrade_nodes':['data-a','data-b','data-d']},lab.atomic.call_args.args[1])
                else:
                    with self.assertRaisesRegex(ValueError,'capacity'):COEXISTENCE.capacity(lab)
                    lab.atomic.assert_not_called()
                self.assertEqual({'selection':'original'},current)
                api.apply.assert_not_called();api.run.assert_not_called()

    def test_business_upgrade_keeps_live_peers_and_uses_source_then_target_product_inputs(self):
        from tests.unit.test_elasticsearch_delivery import configuration, es
        for outcome in ('pass','write-failure','upgrade-failure'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary)
                (state/'coexistence-capacity.json').write_text(json.dumps({'upgrade_nodes':['data-a','data-b','data-c']}))
                configs={name:configuration(name) | {'elasticsearch_namespace':'es-'+name,
                    'elasticsearch_requests':[],'elasticsearch_data_pvc_size':'6Gi'} for name in ('standard','compact')}
                current=dict(configs['standard']);original=copy.deepcopy(current)
                namespaces={};documents={};versions={};inputs=[]
                def config(**updates):current.update(updates);return copy.deepcopy(current)
                def select(name):return config(**configs[name])
                def scenario(ns):
                    namespaces[ns]={'metadata':{'uid':'fixture-uid','resourceVersion':'7','labels':LAB.LABELS}}
                    (state/'namespaces.json').write_text(json.dumps({ns:'fixture-uid'}))
                def get(kind,name='',namespace=''):
                    if kind=='nodes.longhorn.io':
                        return {'items':[{'metadata':{'name':node},'spec':{'disks':{'disk':{}}},
                            'status':{'diskStatus':{'disk':{'storageAvailable':20*1024**3,'storageScheduled':20*1024**3,
                            'storageMaximum':32*1024**3}}}} for node in ('data-a','data-b','data-c')]}
                    return namespaces.get(name)
                def run(*args,body=None,**kwargs):
                    if args[0]=='delete':
                        self.assertEqual('/api/v1/namespaces/es-isolation-upgrade',args[2])
                        self.assertEqual({'uid':'fixture-uid','resourceVersion':'7'},json.loads(body)['preconditions'])
                        del namespaces['es-isolation-upgrade']
                api=SimpleNamespace(get=get,run=Mock(side_effect=run))
                def product(*args,**kwargs):
                    if args[0]=='download':return
                    self.assertEqual(('setup','cluster','07','--tags','elasticsearch'),args)
                    c=config();es.validate(c)
                    manifest=next(obj for obj in es.resources(c) if obj['kind']=='Elasticsearch')
                    self.assertEqual('es-isolation-upgrade',manifest['metadata']['namespace'])
                    self.assertEqual('2Gi',manifest['spec']['nodeSets'][0]['volumeClaimTemplates'][0]['spec']['resources']['requests']['storage'])
                    inputs.append((c['elasticsearch_ver'],c['elasticsearch_state']))
                    if outcome=='upgrade-failure' and c['elasticsearch_ver']=='9.5.6':
                        raise RuntimeError('upgrade failed')
                    versions[c['elasticsearch_namespace']]=manifest['spec']['version']
                @contextmanager
                def connection(c,k):
                    def request(method,path,body=None):
                        if path=='/':return {'version':{'number':versions[c['elasticsearch_namespace']]}}
                        parts=path.strip('/').split('/')
                        if parts[-1]=='_refresh':return {}
                        if len(parts)>1 and parts[1]=='_doc':return {'_source':documents[parts[0]]}
                        if len(parts)>1 and parts[1]=='_create':
                            documents[parts[0]]=body
                            return {'_shards':{'successful':1 if outcome=='write-failure' else 2,'failed':0}}
                        return {'acknowledged':True}
                    yield request
                lab=SimpleNamespace(NAME='cluster',STATE=state,LABELS=LAB.LABELS,NODES=('data-a','data-b','data-c'),atomic=LAB.atomic,api=lambda:api,load_es=lambda:es,
                    configuration=config,selected_config=select,scenario_namespace=scenario,product=product,
                    topology_state=lambda *args,**kwargs:{'cr_uid':'cr','pvc_uids':{'claim':'uid'},
                        'credentials_uid':'secret','credentials_sha256':'hash'},verify_topology=Mock(),data_ledger=Mock())
                with patch.object(es,'connection',connection),patch.object(COEXISTENCE,'verify') as verify:
                    if outcome=='pass':
                        COEXISTENCE.business_upgrade(lab,{})
                        self.assertEqual([('9.5.5','present'),('9.5.6','present'),('9.5.6','absent')],inputs)
                        self.assertFalse(namespaces);self.assertEqual(2,api.run.call_count)
                        self.assertGreaterEqual(verify.call_count,3)
                    else:
                        with self.assertRaisesRegex((ValueError,RuntimeError),'replicated acknowledgement|upgrade failed'):
                            COEXISTENCE.business_upgrade(lab,{})
                        api.run.assert_not_called()
                self.assertEqual(original,current)

    def test_upgrade_storage_checks_replica_placement_before_any_mutation(self):
        from tests.unit.test_elasticsearch_delivery import es
        for capacities,accepted in (([6,6,6],True),([30,30,0],False),([4,6,6],False)):
            with self.subTest(capacities=capacities):
                nodes=[{'metadata':{'name':str(i)},'spec':{'disks':{'d':{'storageReserved':2*1024**3}}},
                    'status':{'diskStatus':{'d':{'storageMaximum':32*1024**3,'storageAvailable':20*1024**3,
                     'storageScheduled':(30-free)*1024**3}}}} for i,free in enumerate(capacities)]
                api=SimpleNamespace(get=Mock(return_value={'items':nodes}),run=Mock(),apply=Mock())
                lab=SimpleNamespace(NODES=('0','1','2'),api=lambda:api,load_es=lambda:es)
                if accepted:COEXISTENCE.upgrade_storage(lab)
                else:
                    with self.assertRaisesRegex(ValueError,'storage cannot admit'):COEXISTENCE.upgrade_storage(lab)
                api.run.assert_not_called();api.apply.assert_not_called()

    def test_failed_upgrade_cleanup_uses_frozen_input_and_bound_namespace_only(self):
        from tests.unit.test_elasticsearch_delivery import configuration,es
        for defect in ('none','uid','labels','namespace','version'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);state=root/'state';state.mkdir();directory=root/'yml/elasticsearch';directory.mkdir(parents=True)
                (state/'namespaces.json').write_text(json.dumps({'es-isolation-upgrade':'owned','peer':'peer'}))
                c=configuration('compact') | {'elasticsearch_namespace':'foreign' if defect=='namespace' else 'es-isolation-upgrade',
                    'elasticsearch_ver':'9.5.4' if defect=='version' else '9.5.5','elasticsearch_data_pvc_size':'6Gi'}
                (directory/'input.json').write_text(json.dumps(c))
                obj={'metadata':{'uid':'foreign' if defect=='uid' else 'owned','resourceVersion':'7',
                    'labels':{} if defect=='labels' else LAB.LABELS}}
                api=SimpleNamespace(get=Mock(return_value=obj),run=Mock())
                current={}
                def config(**updates):current.update(updates);return dict(current)
                def product(*args,**kwargs):
                    es.validate(current)
                    self.assertEqual(('setup','cluster','07','--tags','elasticsearch'),args)
                    self.assertEqual('absent',current['elasticsearch_state'])
                    self.assertEqual('6Gi',current['elasticsearch_data_pvc_size'])
                    self.assertEqual([],current['elasticsearch_requests'])
                lab=SimpleNamespace(NAME='cluster',STATE=state,DIRECTORY=root,LABELS=LAB.LABELS,api=lambda:api,
                    configuration=Mock(side_effect=config),product=Mock(side_effect=product),atomic=LAB.atomic)
                if defect=='none':
                    COEXISTENCE.cleanup_retained_upgrade(lab)
                    self.assertEqual({'peer':'peer'},json.loads((state/'namespaces.json').read_text()))
                    self.assertEqual({'uid':'owned','resourceVersion':'7'},json.loads(api.run.call_args_list[0].kwargs['body'])['preconditions'])
                else:
                    with self.assertRaisesRegex(ValueError,'unbound retained'):COEXISTENCE.cleanup_retained_upgrade(lab)
                    lab.product.assert_not_called();lab.configuration.assert_not_called();api.run.assert_not_called()

    def test_bidirectional_restores_use_real_operations_and_preserve_peer_inputs(self):
        from tests.unit.test_elasticsearch_delivery import configuration, es
        for failed in (False, True, 'analysis-journal', 'analysis-frozen'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary)
                (state/'s3.json').write_text(json.dumps({'bucket':'backups','endpoint':'https://s3.example.com','owner':'gate'}))
                configs={name:configuration(name) | {'elasticsearch_namespace':'es-'+name,
                    'elasticsearch_s3_secret':'source-s3','elasticsearch_requests':[]} for name in ('standard','compact')}
                current=dict(configs['standard']);original=copy.deepcopy(current)
                data={name:{} for name in ('es-standard','es-compact','logging')}
                record={'index':'logging-sentinel','document':{'peer':'logging'}}
                data['logging'][record['index']]={'sentinel':record['document']}
                snapshots={};ledger={};events=[];analysis_failed=[False]
                def config(**updates):current.update(updates);return copy.deepcopy(current)
                def select(name):return config(**configs[name])
                def get(kind,name='',namespace=''):
                    if kind=='namespace':return {'metadata':{'uid':'uid-'+name}}
                    return ledger.get((kind,name,namespace))
                def run(*args,body=None):
                    obj=json.loads(body);obj['metadata'].update(uid='ledger',resourceVersion='1')
                    ledger['configmap',obj['metadata']['name'],'elastic-system']=obj
                api=SimpleNamespace(get=get,run=run)
                def request(namespace,method,path,body=None,*,timeout=60):
                    events.append((namespace,method,path))
                    parts=path.split('?',1)[0].strip('/').split('/')
                    if path=='/':return {'cluster_uuid':'cluster-'+namespace}
                    if parts[0]=='_snapshot':
                        if len(parts)==2 and method=='PUT':return {'acknowledged':True}
                        if parts[-1]=='_verify':return {'nodes':{'one':{}}}
                        if parts[-1]=='_analyze':
                            if isinstance(failed,str) and not analysis_failed[0]:
                                analysis_failed[0]=True
                                raise ValueError('analysis timed out')
                            return {}
                        if len(parts)==2 and method=='DELETE':return {'acknowledged':True}
                        if parts[-1]=='_restore':
                            if namespace=='logging':
                                target=record['index']+'-recovered'
                                data[namespace][target]=copy.deepcopy(data[namespace][record['index']])
                                return {'snapshot':{'shards':{'failed':0,'successful':1}}}
                            if failed is True:raise ValueError('restore execution failed')
                            data[namespace].update(copy.deepcopy(snapshots[parts[2]]['data']))
                            return {'snapshot':{'snapshot':parts[2],'indices':body['indices'].split(','),
                                'shards':{'total':1,'successful':1,'failed':0}}}
                        name=parts[2]
                        if method=='GET':return {'snapshots':[snapshots[name]] if name in snapshots else []}
                        if method=='DELETE':del snapshots[name];return {}
                        indices=body['indices'].split(',')
                        snapshots[name]={'snapshot':name,'uuid':'snapshot-uuid','state':'SUCCESS','indices':indices,
                            'shards':{'total':1,'failed':0},'metadata':body['metadata'],
                            'data':{index:copy.deepcopy(data[namespace][index]) for index in indices}}
                        return {}
                    if parts[:2]==['_cat','indices']:
                        return [{'index':index} for index in data[namespace]]
                    if parts[:2]==['_cluster','health']:return {'status':'green','timed_out':False}
                    index=parts[0]
                    if parts[-1]=='_recovery':
                        return {index:{'shards':[{'primary':True,'stage':'DONE','source':{
                            'repository':'isolation-reader','snapshot':next(iter(snapshots))}}]}}
                    if parts[-1]=='_settings':
                        return {index:{'settings':{'index':{'uuid':'uuid-'+namespace}}}} if index in data[namespace] else {}
                    if parts[-1]=='_refresh':return {}
                    if parts[-1]=='_count':return {'count':len(data[namespace][index])}
                    if len(parts)>1 and parts[1]=='_doc':return {'_source':data[namespace][index][parts[2]]}
                    if len(parts)>1 and parts[1]=='_create':
                        data[namespace][index][parts[2]]=body
                        return {'_shards':{'successful':2,'failed':0}}
                    if method=='PUT':data[namespace][index]={};return {'acknowledged':True}
                    if method=='DELETE':del data[namespace][index];return {'acknowledged':True}
                    raise AssertionError((namespace,method,path))
                @contextmanager
                def connection(c,k):yield lambda *args:request(c['elasticsearch_namespace'],*args)
                @contextmanager
                def logging(_):yield lambda *args:request('logging',*args)
                def product(*args,**kwargs):
                    c=config();es.validate(c)
                    for req in c['elasticsearch_requests']:
                        es.operation(c,api,lambda *values,**options:request(c['elasticsearch_namespace'],*values,**options),req)
                lab=SimpleNamespace(STATE=state,NAME='cluster',api=lambda:api,load_es=lambda:es,configuration=config,
                    selected_config=select,repository_request=LAB.repository_request,product=product,atomic=LAB.atomic,
                    topology_state=lambda _: {'identity':'unchanged'},verify_topology=Mock(),data_ledger=Mock())
                with patch.object(es,'connection',connection),patch.object(COEXISTENCE,'logging_connection',logging), \
                        patch.object(COEXISTENCE,'verify') as verify:
                    if isinstance(failed,str):
                        with self.assertRaisesRegex(ValueError,'analysis timed out'):
                            COEXISTENCE.isolated_restores(lab,record)
                        journal=json.loads((state/'coexistence-restores.json').read_text())
                        self.assertEqual([],journal['completed'])
                        if failed=='analysis-frozen':
                            (state/'coexistence-restores.json').unlink()
                            lab.DIRECTORY=state
                            directory=state/'yml/elasticsearch';directory.mkdir(parents=True)
                            token=journal['token'];repository=LAB.repository_request('standard',json.loads((state/'s3.json').read_text()))
                            (directory/'input.json').write_text(json.dumps({'elasticsearch_requests':[repository,
                                {'id':'isolation-'+token,'type':'snapshot','repository':repository['name'],
                                 'snapshot':'isolation-'+token,'indices':['es-isolation-'+token]}]}))
                        COEXISTENCE.isolated_restores(lab,record,resume=failed=='analysis-frozen')
                        self.assertEqual(1,sum(method=='PUT' and '/_create/' in path for _,method,path in events))
                        self.assertEqual(['snapshot','business','logging','cleanup'],
                            json.loads((state/'coexistence-restores.json').read_text())['completed'])
                        self.assertEqual({},data['es-standard']);self.assertEqual({},data['es-compact'])
                    elif failed:
                        with self.assertRaisesRegex(ValueError,'restore execution failed'):
                            COEXISTENCE.isolated_restores(lab,record)
                    else:
                        COEXISTENCE.isolated_restores(lab,record)
                        self.assertEqual({},data['es-standard']);self.assertEqual({},data['es-compact'])
                        self.assertEqual({},snapshots)
                        self.assertGreaterEqual(verify.call_count,3)
                self.assertEqual(original,current)
                self.assertEqual({record['index']:{'sentinel':record['document']}},data['logging'])
                self.assertFalse(any(ns=='logging' and path=='/'+record['index'] and method=='DELETE'
                    for ns,method,path in events))

    def test_restore_probe_refuses_replaced_uuid_data_or_count_without_mutation(self):
        for defect in ('uuid','data','count','none'):
            with self.subTest(defect=defect):
                def request(method,path,body=None):
                    self.assertEqual('GET',method)
                    if '_settings' in path:return {'probe':{'settings':{'index':{'uuid':'changed' if defect=='uuid' else 'owned'}}}}
                    if '_doc' in path:return {'_source':{'value':2 if defect=='data' else 1}}
                    return {'count':2 if defect=='count' else 1}
                if defect=='none':self.assertEqual('owned',COEXISTENCE.restore_probe(request,'probe',{'value':1},'owned'))
                else:
                    with self.assertRaisesRegex(ValueError,'changed probe'):
                        COEXISTENCE.restore_probe(request,'probe',{'value':1},'owned')

    def test_restores_continuation_persists_only_completed_stages_and_reuses_them(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as temporary,ExitStack() as stack:
            state=Path(temporary);record={'index':'confirmed'}
            (state/'coexistence.json').write_text(json.dumps(record))
            (state/'namespaces.json').write_text(json.dumps({'logging':'owned'}))
            namespace={'metadata':{'uid':'owned','labels':LAB.LABELS}}
            lab=SimpleNamespace(STATE=state,LABELS=LAB.LABELS,baseline=Mock(),configuration=Mock(return_value={}),
                api=lambda:SimpleNamespace(get=lambda *args:namespace),verify_topology=Mock(),data_ledger=Mock(),atomic=LAB.atomic)
            for name in ('verify','repository_refusals','shared_recovery','finish'):
                stack.enter_context(patch.object(COEXISTENCE,name))
            restores=stack.enter_context(patch.object(COEXISTENCE,'isolated_restores'))
            upgrade=stack.enter_context(patch.object(COEXISTENCE,'business_upgrade',side_effect=ValueError('upgrade stopped')))
            reentry=stack.enter_context(patch.object(COEXISTENCE,'business_reentry'))
            reverse=stack.enter_context(patch.object(COEXISTENCE,'reverse'))
            with self.assertRaisesRegex(ValueError,'upgrade stopped'):COEXISTENCE.restores_repair(lab)
            restores.assert_called_once_with(lab,record,resume=True)
            reentry.assert_not_called();reverse.assert_not_called();COEXISTENCE.finish.assert_not_called()
            path=state/'coexistence-isolation-progress.json'
            self.assertEqual(['isolated_restores'],json.loads(path.read_text())['completed'])
            upgrade.side_effect=None
            with redirect_stdout(StringIO()) as output:COEXISTENCE.restores_repair(lab)
            self.assertEqual(1,restores.call_count);reentry.assert_called_once();reverse.assert_called_once()
            COEXISTENCE.repository_refusals.assert_not_called();COEXISTENCE.shared_recovery.assert_not_called()
            self.assertNotIn('ELASTICSEARCH_CASE_PASS',output.getvalue())
            saved=json.loads(path.read_text());saved['completed']=['business_upgrade'];path.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError,'unordered continuation'):COEXISTENCE.restores_repair(lab)
            self.assertEqual(2,upgrade.call_count)

    def test_shared_recovery_waits_for_replacement_readiness_before_product_reentry(self):
        for outcome in ('recovered', 'foreign-owner', 'timeout', 'product-failure'):
            with self.subTest(outcome=outcome):
                def pod(uid, ready, owner='operator'):
                    return {'metadata': {'uid': uid, 'resourceVersion': '3',
                        'ownerReferences': [{'uid': owner}]},
                        'status': {'conditions': [{'type': 'Ready', 'status': ready}]}}
                old = pod('old', 'True', 'foreign' if outcome == 'foreign-owner' else 'operator')
                pending = pod('new', 'False')
                ready = pod('new', 'True')
                values = [old, old, pending, ready]
                current = {'selected': 'original', 'elasticsearch_requests': ['original-request']}
                def get(*args):
                    return values.pop(0) if values else pending
                api = SimpleNamespace(get=Mock(side_effect=get), run=Mock())
                def config(**updates):
                    current.update(updates)
                    return dict(current)
                def product(*args, **kwargs):
                    self.assertFalse(values, 'product reentry preceded replacement readiness')
                    self.assertIn(args[-1],('elasticsearch','logging'))
                    if outcome == 'product-failure': raise RuntimeError('product failure')
                lab = SimpleNamespace(api=lambda: api, NAME='cluster', configuration=config,
                    selected_config=lambda topology: config(selected=topology),
                    product=Mock(side_effect=product), verify_topology=Mock(), data_ledger=Mock(),client_job=Mock())
                ticks = [0, 0, 10, 181] if outcome == 'timeout' else [0, 0, 5, 10]
                with patch.object(COEXISTENCE.time, 'monotonic', side_effect=ticks), \
                        patch.object(COEXISTENCE.time, 'sleep') as sleep, \
                        patch.object(COEXISTENCE,'configure'), \
                        patch.object(COEXISTENCE,'logging_connection',return_value=nullcontext(Mock())), \
                        patch.object(COEXISTENCE,'confirm_write') as write, \
                        patch.object(COEXISTENCE, 'verify') as verify:
                    if outcome == 'foreign-owner':
                        with self.assertRaisesRegex(ValueError, 'unproven Pod owner'):
                            COEXISTENCE.shared_recovery(lab, {'identities': {'operator_uid': 'operator'}})
                        api.run.assert_not_called()
                    elif outcome == 'timeout':
                        with self.assertRaisesRegex(ValueError, '180 seconds'):
                            COEXISTENCE.shared_recovery(lab, {'identities': {'operator_uid': 'operator'}})
                    elif outcome == 'product-failure':
                        with self.assertRaisesRegex(RuntimeError, 'product failure'):
                            COEXISTENCE.shared_recovery(lab, {'identities': {'operator_uid': 'operator'}})
                    else:
                        COEXISTENCE.shared_recovery(lab, {'identities': {'operator_uid': 'operator'}})
                        self.assertEqual(3, lab.product.call_count)
                        self.assertEqual([('standard','business'),('compact','business')],[call.args for call in lab.client_job.call_args_list])
                        write.assert_called_once()
                        self.assertEqual(2, sleep.call_count)
                        verify.assert_called_once()
                    if outcome in ('foreign-owner', 'timeout'):
                        lab.product.assert_not_called(); verify.assert_not_called()
                    self.assertEqual({'selected': 'original', 'elasticsearch_requests': ['original-request']}, current)
                    if outcome != 'foreign-owner':
                        options = json.loads(api.run.call_args.kwargs['body'])
                        self.assertEqual({'uid': 'old', 'resourceVersion': '3'}, options['preconditions'])

    def test_refusal_helper_selects_the_real_product_entry_and_requires_its_exact_error(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)):
            for output,rc,accepted in (('ECK_SHARED_REFUSED: distinct S3 access keys',1,True),
                                       ('ELASTICSEARCH_REFUSED: distinct S3 access keys',1,False),
                                       ('ECK_SHARED_REFUSED: another cause',1,False),
                                       ('ECK_SHARED_REFUSED: distinct S3 access keys',0,False)):
                result=subprocess.CompletedProcess([],rc,output,'')
                with self.subTest(output=output,rc=rc),patch.object(LAB.subprocess,'run',return_value=result) as command:
                    if accepted:LAB.product_refusal('distinct S3 access keys','key-alias',tags='logging',marker='ECK_SHARED_REFUSED')
                    else:
                        with self.assertRaisesRegex(ValueError,'declared cause'):
                            LAB.product_refusal('distinct S3 access keys','key-alias',tags='logging',marker='ECK_SHARED_REFUSED')
                    self.assertEqual(['kubecli','setup',LAB.NAME,'07','--tags','logging'],command.call_args.args[0])

    def test_boundary_refusals_use_real_guards_restore_inputs_and_remove_only_fixture_secrets(self):
        from tests.unit.test_elasticsearch_delivery import configuration, es, eck
        for interrupted in (False, True, 'cleanup-failure'):
            with self.subTest(interrupted=interrupted),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);store={'endpoint':'https://s3.example.com','bucket':'backups','owner':'gate'}
                (state/'s3.json').write_text(json.dumps(store))
                configs={topology:configuration(topology) | {'elasticsearch_namespace':'es-'+topology,
                    'elasticsearch_s3_secret':'source-s3','elasticsearch_requests':[]} for topology in ('standard','compact')}
                current=dict(configs['standard'],logging_namespace='logging',logging_efk_snapshot_secret='logging-snapshot-s3',
                    logging_elasticsearch_ver='9.5.1',logging_kibana_ver='9.5.1')
                initial=copy.deepcopy(current);objects={};refused=[]
                for ns,component,key,name in (('es-standard','elasticsearch','business','source-s3'),
                        ('es-compact','elasticsearch','business','source-s3'),('logging','logging','logging','logging-snapshot-s3')):
                    objects['namespace',ns,'']={'metadata':{'uid':'uid-'+ns,'labels':{'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':component}}}
                    objects['secret',name,ns]={'metadata':{'uid':'original-'+ns,'resourceVersion':'1'},'data':{
                        's3.client.default.access_key':key,'s3.client.default.secret_key':'secret-'+key}}
                    objects['elasticsearch','search' if component=='elasticsearch' else 'logging',ns]={
                        'metadata':{'uid':'cr-'+ns,'labels':{'kubeauto.io/component':'elasticsearch' if component=='elasticsearch' else 'logging-efk'}},
                        'spec':{'secureSettings':[{'secretName':name}]}}
                originals=copy.deepcopy(objects)
                claim={'namespace':'es-standard','namespace_uid':'uid-es-standard','name':configs['standard']['elasticsearch_name'],
                       'repository':'gate-archive','bucket':'backups','base_path':'gate/standard','cluster_uuid':'data-cluster'}
                objects['configmap','kubeauto-eck-repository-writers','elastic-system']={
                    'metadata':{'labels':eck.LABELS},'data':{'claims':json.dumps([claim])}}
                def get(kind,name='',namespace=''):return copy.deepcopy(objects.get((kind,name,namespace)))
                def apply(items):
                    for item in items:
                        obj=copy.deepcopy(item);obj['metadata'].update(uid='fixture-'+obj['metadata']['name'],resourceVersion='7')
                        objects[obj['kind'].lower(),obj['metadata']['name'],obj['metadata']['namespace']]=obj
                def run(*args,body=None):
                    self.assertEqual(('delete','--raw'),args[:2])
                    path=args[2].split('/');ns,name=path[4],path[-1]
                    obj=objects['secret',name,ns]
                    self.assertEqual(obj['metadata']['uid'],json.loads(body)['preconditions']['uid'])
                    if interrupted == 'cleanup-failure':raise ValueError('fixture cleanup failed')
                    del objects['secret',name,ns]
                api=SimpleNamespace(get=get,apply=apply,run=run)
                def config(**updates):current.update(updates);return copy.deepcopy(current)
                def select(topology,**updates):
                    self.assertFalse(updates,'refusal fixtures must not overwrite reusable topology inputs')
                    return config(**configs[topology])
                rows=[{'kind':'Elasticsearch','namespace':ns,'name':name,'uid':obj['metadata']['uid']}
                      for (kind,name,ns),obj in objects.items() if kind=='elasticsearch']
                def product(*args,**kwargs):
                    tags=args[args.index('--tags')+1]
                    if tags=='logging':
                        import yaml
                        from jinja2 import Environment
                        tasks=yaml.safe_load((ROOT/'roles/cluster-addon/tasks/logging.yml').read_text())
                        gate=next(task['ansible.builtin.assert'] for task in tasks
                            if task['name']=='Refuse an unqualified EFK Stack version before shared platform mutation')
                        if not all(Environment().compile_expression(expr)(**({'logging_elasticsearch_ver':'9.5.1',
                                'logging_kibana_ver':'9.5.1'} | current))
                                for expr in gate['that']):
                            raise ValueError(gate['fail_msg'])
                        eck.consumer_preflight(api,current['logging_namespace'],'logging-efk',current['logging_efk_snapshot_secret'])
                    else:
                        es.validate(current)
                        eck.consumer_preflight(api,current['elasticsearch_namespace'],'elasticsearch',current['elasticsearch_s3_secret'])
                        for req in current['elasticsearch_requests']:
                            if req['type']=='repository':es.reserve_repository(current,api,req,commit=True,cluster_uuid='data-cluster')
                def refusal(reason,label,**kwargs):
                    if interrupted and label=='business-credential-alias':raise RuntimeError('interrupted after fixture creation')
                    with self.assertRaisesRegex(ValueError,reason):product('setup','cluster','07','--tags',kwargs.get('tags','elasticsearch'))
                    refused.append(label)
                def topology_state(topology):
                    select(topology)
                    return {'data':'unchanged'}
                lab=SimpleNamespace(STATE=state,NAME='cluster',LABELS=LAB.LABELS,configuration=config,selected_config=select,
                    api=lambda:api,topology_state=topology_state,repository_request=LAB.repository_request,
                    product=product,product_refusal=refusal,verify_topology=lambda _:None,data_ledger=lambda _:None)
                with patch.object(eck,'consumers',return_value=rows),patch.object(COEXISTENCE,'verify'):
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError,'fixture creation'):COEXISTENCE.boundary_refusals(lab,{})
                    else:COEXISTENCE.boundary_refusals(lab,{})
                self.assertEqual(initial,current)
                for key,value in originals.items():self.assertEqual(value,objects[key])
                self.assertEqual(interrupted == 'cleanup-failure',
                    any(key[0]=='secret' and key[1].startswith('es-isolation-') for key in objects))
                self.assertIn('cross-writer',refused)

    def test_reverse_reentry_selects_only_its_delivered_efk_path_from_foreign_outer_flags(self):
        import yaml
        from jinja2 import Environment
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'DIRECTORY',Path(temporary)),patch.object(LAB,'STATE',Path(temporary)/'state'):
            LAB.STATE.mkdir();path=LAB.DIRECTORY/'config.yml'
            path.write_text(yaml.safe_dump(yaml.safe_load((ROOT/'conf/config.yml').read_text()) | {
                'longhorn_install':'yes','elasticsearch_install':'yes','logging_install':'no'}))
            def product(*args,**kwargs):
                current=LAB.configuration();environment=Environment()
                self.assertEqual(('setup',LAB.NAME,'07'),args)
                tasks=yaml.safe_load((ROOT/'roles/cluster-addon/tasks/main.yml').read_text())
                business=next(t for t in tasks if t.get('import_tasks')=='elasticsearch.yml')
                self.assertFalse(environment.compile_expression(business['when'])(**current))
                storage=next(t for t in yaml.safe_load((ROOT/'roles/longhorn/tasks/control.yml').read_text())
                    if t['name']=='Check or reconcile the Longhorn product')
                self.assertFalse(environment.compile_expression(storage['when'])(**current))
                self.assertEqual('yes',current['logging_install'])
                self.assertEqual('local-path',current['logging_efk_storage_class'])
                self.assertEqual('logging-snapshot-s3',current['logging_efk_snapshot_secret'])
            with (patch.object(COEXISTENCE,'unload_efk'),patch.object(COEXISTENCE,'identities',return_value={'owned':True}),
                  patch.object(COEXISTENCE,'minio_job'),patch.object(COEXISTENCE,'verify'),
                  patch.object(LAB,'verify_topology'),patch.object(LAB,'data_ledger'),patch.object(LAB,'product',side_effect=product)):
                COEXISTENCE.reverse(LAB,{'identities':{'owned':True}})

    def test_retained_fixture_resumes_only_reverse_with_storage_disabled_and_no_case_acceptance(self):
        import yaml
        from jinja2 import Environment
        import re
        for outcome in ('pass','retired','retired-continuation','setup-failure','foreign-namespace','partial-retirement'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);state=root/'state';state.mkdir()
                original={key:value for key,value in yaml.safe_load((ROOT/'conf/config.yml').read_text()).items() if key.startswith('logging_')}
                original.update(longhorn_install='yes',elasticsearch_install='yes',logging_install='no',prom_install='yes',ingress_nginx_install='no')
                (root/'config.yml').write_text(yaml.safe_dump(original))
                (state/'coexistence.json').write_text(json.dumps({'identities':{'retained':'identities'}}))
                (state/'namespaces.json').write_text(json.dumps({'logging':'owned'}))
                objects=self.objects()
                ns={'metadata':{'uid':'foreign' if outcome=='foreign-namespace' else 'owned','labels':LAB.LABELS}}
                def get(kind,*args):
                    if kind in objects:
                        if outcome in ('retired','retired-continuation') or (outcome=='partial-retirement' and kind=='kibana'):return None
                        return objects[kind]
                    return ns
                api=SimpleNamespace(get=get,run=Mock(side_effect=lambda *args,**kw:json.dumps(objects[args[3]]) if 'get' in args else ''))
                def product(*args,**kwargs):
                    self.assertEqual(('setup',LAB.NAME,'07'),args)
                    current=yaml.safe_load((root/'config.yml').read_text())
                    self.assertEqual('no',current['longhorn_install'])
                    self.assertEqual('no',current['elasticsearch_install'])
                    self.assertEqual('yes',current['logging_install'])
                    environment=Environment()
                    environment.tests['match']=lambda value,pattern:bool(re.match(pattern,value))
                    environment.filters['unique']=lambda values:list(dict.fromkeys(values))
                    assertions=yaml.safe_load((ROOT/'roles/cluster-addon/tasks/logging.yml').read_text())[0]['ansible.builtin.assert']['that']
                    for assertion in assertions:
                        self.assertTrue(environment.compile_expression(assertion)(**current),assertion)
                    if outcome=='setup-failure':raise ValueError('setup failed')
                with (patch.object(LAB,'STATE',state),patch.object(LAB,'DIRECTORY',root),
                      patch.object(LAB,'api',return_value=api),patch.object(LAB,'baseline'),
                      patch.object(LAB,'verify_topology') as topology,patch.object(LAB,'data_ledger'),
                      patch.object(LAB,'product',side_effect=product),patch.object(LAB,'selected_config') as selected,
                      patch.object(COEXISTENCE,'fixtures') as fixtures,patch.object(COEXISTENCE,'verify'),
                      patch.object(COEXISTENCE,'identities',return_value={'retained':'identities'}),
                      patch.object(COEXISTENCE,'minio_job'),redirect_stderr(StringIO())):
                    output=StringIO()
                    from contextlib import redirect_stdout
                    with redirect_stdout(output):
                        if outcome=='retired-continuation':
                            (state/'coexistence-isolation-progress.json').write_text(json.dumps({
                                'logging':{'identities':{'retained':'identities'}},
                                'completed':['isolated_restores','business_upgrade','business_reentry']}))
                            COEXISTENCE.restores_repair(LAB)
                            self.assertEqual(4,len(json.loads((state/'coexistence-isolation-progress.json').read_text())['completed']))
                        elif outcome in ('pass','retired','retired-continuation'):COEXISTENCE.run(LAB)
                        else:
                            with self.assertRaisesRegex(ValueError,'setup failed|unbound or replaced|partial retirement'):COEXISTENCE.run(LAB)
                    fixtures.assert_not_called();selected.assert_not_called()
                    self.assertNotIn('ELASTICSEARCH_CASE_PASS',output.getvalue())
                    if outcome in ('pass','retired','retired-continuation'):
                        self.assertIn('ELASTICSEARCH_COEXISTENCE_REPAIR_PASS',output.getvalue())
                        self.assertEqual(2 if outcome in ('retired','retired-continuation') else 4,sum(call.args[0]=='delete' and '--raw' in call.args for call in api.run.call_args_list))
                    elif outcome in ('foreign-namespace','partial-retirement'):
                        topology.assert_not_called();api.run.assert_not_called()
                self.assertEqual(original,yaml.safe_load((root/'config.yml').read_text()))

    def test_retirement_reads_managed_fields_through_the_real_product_api(self):
        objects = self.objects()
        with patch.object(LAB, 'BASE', ROOT):
            product = LAB.load_es()
        def kubectl(argv, **kwargs):
            if 'get' in argv:
                obj = copy.deepcopy(objects[argv[argv.index('get')+1]])
                if '--show-managed-fields=true' not in argv:
                    obj['metadata'].pop('managedFields')
                return subprocess.CompletedProcess(argv, 0, json.dumps(obj), '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(product.subprocess, 'run', side_effect=kubectl) as command:
            COEXISTENCE.unload_efk(SimpleNamespace(api=lambda:product.API('owned-context')))
        reads = [call.args[0] for call in command.call_args_list if 'get' in call.args[0]]
        self.assertEqual(2, len(reads))
        self.assertTrue(all('--show-managed-fields=true' in argv for argv in reads))

    def test_addon_handoff_reuses_storage_and_restores_its_config_after_setup(self):
        import yaml
        from jinja2 import Environment
        class BoundaryReached(Exception): pass
        for failed_setup in (False, True):
            with (tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'DIRECTORY', Path(temporary)),
                  patch.object(LAB,'STATE',Path(temporary)/'state')):
                LAB.STATE.mkdir()
                plan={'upgrade_nodes':list(LAB.NODES[3:6]),'logging_nodes':list(LAB.NODES[:3])}
                (LAB.STATE/'coexistence-capacity.json').write_text(json.dumps(plan))
                config = Path(temporary)/'config.yml'
                config.write_text('longhorn_install: "yes"\nelasticsearch_install: "yes"\nlogging_install: "no"\n')
                logging_queries=iter([None,{'metadata':{'uid':'owned'}}])
                k = Mock(); k.get.side_effect=lambda *args,**kwargs: next(logging_queries) if args==('namespace','logging') else {'metadata':{'uid':'owned'}}
                def execute(argv, **kwargs):
                    if argv[1] == 'download': return ''
                    self.assertEqual(['kubecli', 'setup', LAB.NAME, '07'], argv)
                    current = yaml.safe_load(config.read_text())
                    self.assertEqual('no', current['longhorn_install'])
                    tasks=yaml.safe_load((ROOT/'roles/cluster-addon/tasks/main.yml').read_text())
                    business=next(task for task in tasks if task.get('import_tasks')=='elasticsearch.yml')
                    self.assertFalse(Environment().compile_expression(business['when'])(**current))
                    self.assertEqual('yes', current['logging_install'])
                    task = next(row for row in yaml.safe_load((ROOT/'roles/longhorn/tasks/control.yml').read_text())
                                if row['name'] == 'Check or reconcile the Longhorn product')
                    self.assertFalse(Environment().compile_expression(task['when'])(**current))
                    if failed_setup: raise BoundaryReached()
                    return ''
                with (patch.object(LAB, 'baseline'), patch.object(LAB, 'api', return_value=k),
                      patch.object(LAB, 'topology_state', return_value={}), patch.object(LAB, 'stage_images'),
                      patch.object(LAB,'selected_config',return_value={'elasticsearch_data_nodes':plan['upgrade_nodes']}),
                      patch.object(LAB, 'execute', side_effect=execute), patch.object(COEXISTENCE, 'fixtures'),
                      patch.object(COEXISTENCE, 'capacity'),
                      patch.object(COEXISTENCE, 'logging_connection', side_effect=BoundaryReached)):
                    with self.assertRaises(BoundaryReached): COEXISTENCE.run(LAB)
                current = yaml.safe_load(config.read_text())
                self.assertEqual('yes', current['longhorn_install'])
                self.assertEqual('no', current['logging_install'])

    def objects(self):
        return {kind:{'metadata':{'uid':kind+'-uid','resourceVersion':'7','labels':{
            'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'logging-efk'},
            'managedFields':[{'manager':'kubeauto-logging-efk'}]}} for kind in ('elasticsearch','kibana')}

    def test_all_cr_owners_are_checked_before_first_retirement(self):
        objects=self.objects();objects['elasticsearch']['metadata']['managedFields']=[]
        api=SimpleNamespace(run=Mock(side_effect=lambda *args,**kwargs:json.dumps(objects[args[3]]) if 'get' in args else ''))
        with self.assertRaisesRegex(ValueError,'ownership is unproven'):COEXISTENCE.unload_efk(SimpleNamespace(api=lambda:api))
        self.assertFalse(any(call.args[0]=='delete' for call in api.run.call_args_list))

    def test_retirement_uses_both_resource_preconditions_and_never_deletes_claims(self):
        objects=self.objects();api=SimpleNamespace(run=Mock(side_effect=lambda *args,**kwargs:json.dumps(objects[args[3]]) if 'get' in args else ''))
        COEXISTENCE.unload_efk(SimpleNamespace(api=lambda:api))
        deletions=[call for call in api.run.call_args_list if call.args[0]=='delete']
        self.assertEqual(2,len(deletions))
        for call in deletions:
            kind='kibana' if '/kibanas/' in call.args[2] else 'elasticsearch'
            self.assertEqual({'uid':kind+'-uid','resourceVersion':'7'},json.loads(call.kwargs['body'])['preconditions'])
        self.assertFalse(any('pvc' in str(call) or 'secret' in str(call) for call in api.run.call_args_list))


class WorkloadCleanupContracts(unittest.TestCase):
    def test_mmap_acceptance_requires_both_runtime_and_persistent_thresholds(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)),patch.object(LAB,'baseline',return_value={'cluster_uid':'bound'}):
            c={'elasticsearch_data_nodes':list(LAB.NODES[3:]),'elasticsearch_master_nodes':list(LAB.NODES[:3])}
            with patch.object(LAB,'host',return_value=json.dumps({'runtime':1048576,'persistent':'vm.max_map_count=262144\n'})):
                with self.assertRaisesRegex(ValueError,'persistent setting'):LAB.verify_mmap(c)
            self.assertFalse((Path(temporary)/'mmap.json').exists())
            with patch.object(LAB,'host',return_value=json.dumps({'runtime':1048576,'persistent':'vm.max_map_count=1048576\n'})):
                LAB.verify_mmap(c)
            record=json.loads((Path(temporary)/'mmap.json').read_text())
            self.assertEqual(set(LAB.NODES),set(record['nodes']))

    def test_final_cleanup_refuses_unarchived_state_before_final_host_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);state=root/'state';state.mkdir();(state/'foreign-receipt').write_text('preserve')
            with patch.object(LAB,'BASE',root),patch.object(LAB,'STATE',state),patch.object(LAB,'cleanup'),patch.object(LAB,'execute') as execute:
                with self.assertRaisesRegex(ValueError,'unarchived owned state'):LAB.cleanup_gate()
                execute.assert_not_called()

    def test_all_namespace_identities_precede_deletion_and_storage_controllers_survive_until_pvs_exit(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)):
            records={'monitor':'monitor-uid','logging':'logging-uid','local-path-storage':'storage-uid',
                     'elastic-system':'eck-uid','longhorn-system':'longhorn-uid'}
            (Path(temporary)/'namespaces.json').write_text(json.dumps(records))
            objects={name:{'metadata':{'uid':uid,'resourceVersion':'12'}} for name,uid in records.items()}
            api=SimpleNamespace(get=lambda kind,name:objects[name],run=Mock())
            with patch.object(LAB,'api',return_value=api),patch.object(LAB,'execute') as execute:
                objects['logging']['metadata']['uid']='foreign'
                with self.assertRaisesRegex(ValueError,'replaced namespace'):LAB.cleanup_workload_namespaces()
                api.run.assert_not_called();execute.assert_not_called()
                objects['logging']['metadata']['uid']='logging-uid'
                LAB.cleanup_workload_namespaces()
                deletions=[c for c in api.run.call_args_list if c.args[0]=='delete']
                self.assertEqual({'/api/v1/namespaces/monitor','/api/v1/namespaces/logging'},{c.args[2] for c in deletions})
                for call in deletions:self.assertEqual('12',json.loads(call.kwargs['body'])['preconditions']['resourceVersion'])
                self.assertIn('can-remove',execute.call_args.args[0])
        source=(ROOT/'tests/helpers/elasticsearch-lab.py').read_text().split('\ndef cleanup():',1)[1].split('\ndef main():',1)[0]
        self.assertLess(source.index('cleanup_workload_namespaces()'),source.index("while k.get('pv')"))
        self.assertLess(source.index("while k.get('pv')"),source.index("longhorn_action='uninstall'"))


class LoggingHandoffContracts(unittest.TestCase):
    def conflict_api(self, objects):
        def get(kind,name='',namespace=''):
            if kind=='crd':return {'items':[]}
            return copy.deepcopy(objects.get((kind,name,namespace)))
        def apply(rows):
            for row in rows:
                obj=copy.deepcopy(row);meta=obj['metadata'];kind=obj['kind'].lower()
                key=(kind,meta['name'],meta.get('namespace',''))
                if key in objects:raise AssertionError('fixture must not overwrite existing resources')
                meta['uid']=kind+'-uid'
                if 'stringData' in obj:
                    import base64
                    obj['data']={key:base64.b64encode(value.encode()).decode() for key,value in obj.pop('stringData').items()}
                objects[key]=obj
        return SimpleNamespace(get=get,apply=Mock(side_effect=apply))

    def test_loki_conflict_fixture_reaches_real_ownership_refusal_after_real_shared_preflight(self):
        import os
        import yaml
        from jinja2 import Environment
        spec=importlib.util.spec_from_file_location('logging_conflict_eck',ROOT/'roles/cluster-addon/files/eck.py')
        eck=importlib.util.module_from_spec(spec);spec.loader.exec_module(eck)
        labels={'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'logging','kubeauto.io/logging-solution':'loki'}
        objects={('namespace','logging',''):{'metadata':{'uid':'namespace-uid','labels':labels}}}
        api=self.conflict_api(objects);record={'source_owner':'owner'}
        with self.assertRaisesRegex(ValueError,'S3 credentials must exist'):
            eck.consumer_preflight(api,'logging','logging-efk','logging-snapshot-s3')
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)):
            LAB.logging_conflict_fixture(api,record)
            eck.consumer_preflight(api,'logging','logging-efk','logging-snapshot-s3')
            before=copy.deepcopy(objects);LAB.logging_conflict_fixture(api,record)
            self.assertEqual(before,objects);self.assertEqual(1,api.apply.call_count)
            tasks=yaml.safe_load((ROOT/'roles/cluster-addon/tasks/logging.yml').read_text())
            task=next(row for row in tasks if row.get('name')=='Discover existing kubeauto logging ownership before mutation')
            env=Environment();env.filters['quote']=lambda value:__import__('shlex').quote(value)
            script=env.from_string(task['ansible.builtin.shell']).render(cluster_dir='/fixture',logging_namespace='logging',logging_solution='efk')
            root=Path(temporary);binary=root/'kubectl'
            binary.write_text('#!/bin/sh\nprintf loki\n');binary.chmod(0o700)
            result=subprocess.run(['bash','-c',script],env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH']),capture_output=True,text=True)
            self.assertEqual(42,result.returncode,result.stderr)
            self.assertIn('existing kubeauto logging solution=loki, requested=efk',result.stderr)
            self.assertEqual(before,objects)
            self.assertEqual(record,json.loads((root/'logging-ownership.json').read_text()))

    def test_conflict_fixture_creates_only_owned_objects_and_refuses_identity_drift(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)):
            objects={};api=self.conflict_api(objects);record={'source_owner':'owner'}
            LAB.logging_conflict_fixture(api,record)
            original=copy.deepcopy(objects);saved=copy.deepcopy(record)
            for fault in ('foreign-namespace','foreign-secret','secret-uid','namespace-uid','lost-secret','lost-namespace','missing-key','missing-binding'):
                with self.subTest(fault=fault):
                    objects.clear();objects.update(copy.deepcopy(original));record=copy.deepcopy(saved)
                    ns=objects[('namespace','logging','')];secret=objects[('secret','logging-snapshot-s3','logging')]
                    if fault=='foreign-namespace':ns['metadata']['labels']['kubeauto.io/component']='foreign'
                    elif fault=='foreign-secret':secret['metadata']['labels']['kubeauto.io/es-conflict-fixture']='foreign'
                    elif fault=='secret-uid':secret['metadata']['uid']='foreign'
                    elif fault=='namespace-uid':ns['metadata']['uid']='foreign'
                    elif fault=='lost-secret':del objects[('secret','logging-snapshot-s3','logging')]
                    elif fault=='lost-namespace':del objects[('namespace','logging','')]
                    elif fault=='missing-key':secret['data'].pop('s3.client.default.access_key')
                    else:record.pop('loki_conflict_fixture')
                    before=copy.deepcopy(objects);api.apply.reset_mock()
                    with self.assertRaises(ValueError):LAB.logging_conflict_fixture(api,record)
                    self.assertEqual(before,objects);api.apply.assert_not_called()

    def test_completed_route_prefix_admits_only_the_next_owned_namespace(self):
        labels={'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'logging','kubeauto.io/logging-solution':'loki'}
        objects={('namespace','logging',''):{'metadata':{'uid':'loki','labels':labels}}};api=self.conflict_api(objects)
        record={'routes':{'efk-direct':{},'efk-kafka-buffer':{}}}
        LAB.logging_retained_route(api,record)
        for routes in ({'efk-kafka-buffer':{}},{'loki-direct':{}},{**record['routes'],'loki-direct':{}}):
            with self.subTest(routes=routes),self.assertRaises(ValueError):LAB.logging_retained_route(api,{'routes':routes})
        objects[('namespace','logging','')]['metadata']['labels']['kubeauto.io/logging-solution']='efk'
        with self.assertRaisesRegex(ValueError,'next-route namespace'):LAB.logging_retained_route(api,record)

    def test_real_logging_continuation_reuses_both_efk_receipts_before_loki_without_global_empty_check(self):
        labels={'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'logging','kubeauto.io/logging-solution':'loki'}
        objects={('namespace','logging',''):{'metadata':{'uid':'loki','labels':labels}},
                 ('namespace','kube-system',''):{'metadata':{'uid':'cluster'}}};api=self.conflict_api(objects)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);state=root/'state';state.mkdir();(root/'clusters/logging-gate').mkdir(parents=True)
            record={'phase':'source-clean','source_owner':'owner','cluster_uid':'cluster','routes':{},
                    'hardware':{ip:{'product_uuid':ip} for ip in LAB.PREVIOUS_HOSTS}}
            inputs={'business':'same'}
            for route in ('efk-direct','efk-kafka-buffer'):
                stem=state/route;log=stem.with_suffix('.log');log.write_text('LOGGING_FULL_GATE_PASS\n')
                for suffix in ('.exit','.finalized'):Path(str(stem)+suffix).write_text('0')
                record['routes'][route]={'inputs':inputs,'durable':str(stem),'log':str(log),'sha256':EVIDENCE.digest(log)}
            LAB.atomic(state/'logging-ownership.json',record)
            def launch(command,log,environment,timeout):
                self.assertEqual('loki',environment['LOGGING_SOLUTION'])
                self.assertEqual('no',environment['LOGGING_PLATFORM_RESUME'])
                self.assertIn(('secret','logging-snapshot-s3','logging'),objects)
                raise ValueError('next Loki route reached')
            with (patch.object(LAB,'BASE',root),patch.object(LAB,'STATE',state),patch.object(LAB,'stage_images'),
                  patch.object(LAB,'host_capacity',side_effect=lambda ip:{'product_uuid':ip}),
                  patch.object(LAB,'load_es',return_value=SimpleNamespace(API=lambda _:api)),
                  patch.object(LAB,'logging_platform_resume_needed',return_value=False),
                  patch.object(LAB,'logging_inputs',return_value=inputs),patch.object(LAB,'execute') as execute,
                  patch.object(LAB,'stream_command',side_effect=launch) as stream,redirect_stdout(StringIO()) as output):
                with self.assertRaisesRegex(ValueError,'next Loki route reached'):LAB.logging()
                execute.assert_not_called();self.assertEqual(1,stream.call_count)
            for route in record['routes']:self.assertIn('ELASTICSEARCH_LOGGING_ROUTE_REUSED route='+route,output.getvalue())
        with patch.object(LAB,'BASE',ROOT):
            self.assertNotIn('tests/helpers/elasticsearch-lab.py',LAB.logging_inputs())

    def test_logging_fixture_declares_ownership_before_shared_eck_preflight(self):
        import os
        raw=(ROOT/'tests/helpers/logging-regression.sh').read_text()
        start=raw.index('kubectl create namespace logging --dry-run=client')
        end=raw.index('if [[ "$LOGGING_SOLUTION" == efk ]]; then',start)
        fixture=raw[start:end]
        self.assertLess(end,raw.index('echo LOGGING_STAGE_BEGIN kubeauto-addon',end))
        spec=importlib.util.spec_from_file_location('logging_fixture_eck',ROOT/'roles/cluster-addon/files/eck.py')
        eck=importlib.util.module_from_spec(spec);spec.loader.exec_module(eck)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'namespace.json';binary=root/'kubectl'
            binary.write_text('#!'+sys.executable+'\nimport json,os,sys\nfrom pathlib import Path\n'
                'path=Path(os.environ["FIXTURE_NAMESPACE"]);args=sys.argv[1:]\n'
                'if args[:3]==["create","namespace","logging"]:\n'
                ' print(json.dumps({"metadata":{"name":"logging"}}))\n'
                'elif args[:1]==["apply"]:\n'
                ' obj=json.load(sys.stdin)\n'
                ' if not path.exists():path.write_text(json.dumps(obj))\n'
                'elif args[:3]==["label","namespace","logging"]:\n'
                ' obj=json.loads(path.read_text());labels=obj["metadata"].setdefault("labels",{})\n'
                ' for arg in args[3:]:\n'
                '  key,value=arg.split("=",1)\n'
                '  if key in labels and labels[key]!=value:sys.exit(1)\n'
                '  labels[key]=value\n'
                ' path.write_text(json.dumps(obj))\n'
                'else:sys.exit(2)\n')
            binary.chmod(0o700)
            environment=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],
                             FIXTURE_NAMESPACE=str(path),LOGGING_SOLUTION='efk')
            api=SimpleNamespace(get=lambda kind,*args:json.loads(path.read_text()))
            with patch.object(eck,'consumers',return_value=[]):
                path.write_text(json.dumps({'metadata':{'name':'logging'}}))
                with self.assertRaisesRegex(ValueError,'consumer namespace belongs'):
                    eck.consumer_preflight(api,'logging','logging-efk')
                result=subprocess.run(['bash','-euo','pipefail','-c',fixture],env=environment,capture_output=True,text=True)
                self.assertEqual(0,result.returncode,result.stderr)
                eck.consumer_preflight(api,'logging','logging-efk')
                before={'metadata':{'name':'logging','labels':{'kubeauto.io/component':'elasticsearch'}}}
                path.write_text(json.dumps(before))
                result=subprocess.run(['bash','-euo','pipefail','-c',fixture],env=environment,capture_output=True,text=True)
                self.assertNotEqual(0,result.returncode)
                self.assertEqual(before,json.loads(path.read_text()))

    def test_completed_business_survives_cleanup_failure_and_retries_only_cleanup(self):
        def completed(root,route,suffix,inputs):
            stem=root/('logging-'+route+'-'+suffix)
            log=Path(str(stem)+'.log')
            log.write_text('LOGGING_CASE_PASS id=LOGGING-26 route='+route+' detail=verified\nLOGGING_FULL_GATE_PASS\n')
            for ending in ('.exit','.finalized'):Path(str(stem)+ending).write_text('0')
            return {'durable':str(stem),'log':str(log),'sha256':EVIDENCE.digest(log),'inputs':inputs}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);state=root/'state';state.mkdir();directory=root/'clusters/logging-gate';directory.mkdir(parents=True)
            (directory/'kubectl.kubeconfig').write_text('bound fixture')
            inputs={'business':'same'}
            direct=completed(state,'efk-direct','a'*12,dict(inputs,**{'tests/helpers/kafka-cleanup.sh':'old-cleanup'}))
            kafka=completed(state,'efk-kafka-buffer','b'*12,direct['inputs'])
            record={'phase':'source-clean','source_owner':'owner','cluster_uid':'cluster','hosts':list(LAB.PREVIOUS_HOSTS),
                    'hardware':{ip:{'product_uuid':ip} for ip in LAB.PREVIOUS_HOSTS},'routes':{'efk-direct':direct}}
            path=state/'logging-ownership.json';LAB.atomic(path,record)
            events=[]
            def cleanup(command,**kwargs):
                name=str(command[-1])
                events.append(tuple(str(x) for x in command))
                if name.endswith('kafka-cleanup.sh'):
                    saved=json.loads(path.read_text())
                    self.assertEqual('efk-kafka-buffer',saved['pending_route']['route'])
                    if fail_cleanup[0]:raise ValueError('fixture cleanup refusal')
            def next_route(command,log,environment,timeout):
                self.assertEqual('loki',environment['LOGGING_SOLUTION'])
                raise ValueError('fixture reached next independent route')
            fake=SimpleNamespace(get=lambda kind,name='',*args,**kwargs:
                {'metadata':{'uid':'cluster'}} if kind=='namespace' and name=='kube-system' else None)
            fail_cleanup=[True]
            with (patch.object(LAB,'BASE',root),patch.object(LAB,'STATE',state),
                  patch.object(LAB,'host_capacity',side_effect=lambda ip:{'product_uuid':ip}),
                  patch.object(LAB,'load_es',return_value=SimpleNamespace(API=lambda _:fake)),
                  patch.object(LAB,'logging_platform_resume_needed',return_value=False),patch.object(LAB,'stage_images'),
                  patch.object(LAB,'logging_conflict_fixture'),
                  patch.object(LAB,'logging_inputs',return_value=inputs),patch.object(LAB,'execute',side_effect=cleanup),
                  patch.object(LAB,'stream_command',side_effect=next_route) as launch,redirect_stdout(StringIO())):
                with self.assertRaisesRegex(ValueError,'fixture cleanup refusal'):LAB.logging()
                launch.assert_not_called()
                saved=json.loads(path.read_text());self.assertEqual({'efk-direct'},set(saved['routes']))
                self.assertEqual(kafka['log'],saved['pending_route']['receipt']['log'])
                fail_cleanup[0]=False
                with self.assertRaisesRegex(ValueError,'next independent route'):LAB.logging()
                self.assertEqual(1,launch.call_count)
                saved=json.loads(path.read_text());self.assertNotIn('pending_route',saved)
                self.assertEqual({'efk-direct','efk-kafka-buffer'},set(saved['routes']))
            self.assertEqual('0',Path(kafka['durable']+'.exit').read_text())
            self.assertEqual(2,sum(row[-1].endswith('kafka-cleanup.sh') for row in events))

    def test_pending_business_refuses_changed_owner_cluster_inputs_and_durable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);stem=root/'route';log=root/'route.log';log.write_text('LOGGING_FULL_GATE_PASS\n')
            for ending in ('.exit','.finalized'):Path(str(stem)+ending).write_text('0')
            receipt={'durable':str(stem),'log':str(log),'sha256':EVIDENCE.digest(log),'inputs':{'business':'same'}}
            record={'source_owner':'owner','cluster_uid':'cluster','pending_route':{
                'source_owner':'owner','cluster_uid':'cluster','route':'efk-kafka-buffer','receipt':receipt}}
            self.assertEqual(receipt,LAB.logging_pending_route(record,'efk-kafka-buffer',{'business':'same'}))
            for key in ('source_owner','cluster_uid','route'):
                changed=copy.deepcopy(record);changed['pending_route'][key]='foreign'
                with self.assertRaisesRegex(ValueError,'ownership changed'):LAB.logging_pending_route(changed,'efk-kafka-buffer',{'business':'same'})
            with self.assertRaisesRegex(ValueError,'inputs changed'):LAB.logging_pending_route(record,'efk-kafka-buffer',{'business':'changed'})
            Path(str(stem)+'.finalized').write_text('1')
            with self.assertRaisesRegex(ValueError,'durable fence'):LAB.logging_pending_route(record,'efk-kafka-buffer',{'business':'same'})

    def test_minio_fixture_upload_is_exact_and_precedes_any_fixture_pod(self):
        import os
        raw=(ROOT/'tests/helpers/logging-regression.sh').read_text()
        functions=raw[raw.index('fail() {'):raw.index('case_pass() {')]
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'calls'
            script=functions+'\ndocker() { printf "%s\\n" "$*" >> "$FIXTURE_CALLS"; }\nlogging_upload_fixtures\n'
            result=subprocess.run(['bash','-eu','-c',script],env=dict(os.environ,FIXTURE_CALLS=str(path)),text=True,capture_output=True)
            self.assertEqual(0,result.returncode,result.stderr)
            expected=[]
            for image in ('minio:RELEASE.2025-04-08T15-41-24Z','minio-mc:RELEASE.2025-04-08T15-39-49Z'):
                expected.extend(['image inspect brinnatt/'+image,'tag brinnatt/'+image+' 127.0.0.1:5000/brinnatt/'+image,'push 127.0.0.1:5000/brinnatt/'+image])
            self.assertEqual(expected,path.read_text().splitlines())
        upload=raw.index('  logging_upload_fixtures\n')
        manifest=raw.index('prometheus-config-reloader:v0.91.0 minio:RELEASE.')
        self.assertLess(upload,manifest)
        self.assertLess(manifest,raw.index('kubectl create deployment minio'))

    def test_bootstrap_resume_requires_bound_empty_platform_and_exact_nodes(self):
        import yaml
        for fault in ('','uid','namespace','storage','node','workload','inputs','routes'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary)
                config={'K8S_VER':'1.33.6','REGISTRY_HOST_IP':'192.168.122.243','logging_install':'no'}
                if fault=='inputs':config['REGISTRY_HOST_IP']='192.168.122.1'
                (directory/'config.yml').write_text(yaml.safe_dump(config))
                record={'cluster_uid':'bound','routes':{'efk-direct':{}} if fault=='routes' else {}}
                namespaces=[{'metadata':{'name':name}} for name in ('default','kube-system','kube-public','kube-node-lease')]
                if fault=='namespace':namespaces.append({'metadata':{'name':'foreign'}})
                nodes=[{'status':{'addresses':[{'type':'InternalIP','address':ip}],'nodeInfo':{'kubeletVersion':'v1.33.6'}}} for ip in LAB.PREVIOUS_HOSTS]
                if fault=='node':nodes.pop()
                pod={'metadata':{'namespace':'kube-system','labels':{'k8s-app':'foreign' if fault=='workload' else 'calico-node'}}}
                values={'namespace':{'metadata':{'uid':'foreign' if fault=='uid' else 'bound'}},'namespaces':{'items':namespaces},
                        'nodes':{'items':nodes},'pv':{'items':[{}] if fault=='storage' else []}}
                api=SimpleNamespace(get=lambda kind,*args:values.get(kind),run=lambda *args:json.dumps({'items':[pod] if 'pods' in args else []}))
                with patch.object(LAB,'load_es',return_value=SimpleNamespace(API=lambda *args:api)):
                    if fault:
                        with self.assertRaises(ValueError):LAB.logging_platform_resume_needed(record,directory)
                    else:self.assertTrue(LAB.logging_platform_resume_needed(record,directory))

    def test_registry_input_and_resume_execute_real_configuration_and_hosts_payload(self):
        import os
        raw=(ROOT/'tests/helpers/logging-regression.sh').read_text()
        functions=raw[raw.index('fail() {'):raw.index('case_pass() {')]
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);binpath=root/'bin';binpath.mkdir();config=root/'config.yml'
            config.write_text('REGISTRY_HOST_IP: "192.168.122.243"\nprom_install: "no"\nlocal_path_provisioner_install: "no"\nlogging_install: "no"\n')
            for ip in LAB.PREVIOUS_HOSTS:
                (root/(ip+'-hosts')).write_text('127.0.0.1 localhost\n192.168.122.243 registry.talkschool.cn\n')
                (root/(ip+'-before')).write_text((root/(ip+'-hosts')).read_text())
            ssh=binpath/'ssh'
            ssh.write_text('#!'+sys.executable+'\nimport os,sys\nfrom pathlib import Path\n'
                'root=Path(os.environ["FIXTURE_ROOT"]);ip=next(a[5:] for a in sys.argv if a.startswith("root@"))\n'
                'source=sys.stdin.read().replace("Path(\'/etc/hosts\')","Path("+repr(str(root/(ip+"-hosts")))+")")\n'
                'source=source.replace("Path(\'/var/tmp/kubeauto-logging-hosts.before\')","Path("+repr(str(root/(ip+"-before")))+")")\n'
                'sys.argv=["-",sys.argv[-1]]\nexec(compile(source,"real-registry-payload","exec"))\n')
            ssh.chmod(0o700)
            cli=binpath/'kubecli'
            cli.write_text('#!'+sys.executable+'\nimport json,os,sys,yaml\nfrom pathlib import Path\n'
                'root=Path(os.environ["FIXTURE_ROOT"]);c=yaml.safe_load((root/"config.yml").read_text())\n'
                'assert c["REGISTRY_HOST_IP"]=="192.168.122.2" and c["prom_install"]=="yes" and c["local_path_provisioner_install"]=="yes" and c["prom_storage_class"]=="local-path" and c["logging_install"]=="no"\n'
                'with (root/"calls").open("a") as output:output.write(json.dumps(sys.argv[1:])+"\\n")\n')
            cli.chmod(0o700)
            script=functions+'\ncurl() { [[ "${FAIL_PREFLIGHT:-no}" != yes ]]; }\nCFG="$FIXTURE_ROOT/config.yml"\nK="$FIXTURE_ROOT/bin/kubecli"\nCLUSTER=logging-gate\nlogging_platform_resume\n'
            env=dict(os.environ,FIXTURE_ROOT=str(root),PATH=str(binpath)+':'+os.environ['PATH'],LOGGING_REGISTRY_HOST_IP='192.168.122.2',
                     LOGGING_PLATFORM_RESUME='yes',ELASTICSEARCH_IMAGE_RECEIPT='bound-parent-receipt')
            failed=subprocess.run(['bash','-eu','-c',script],env=dict(env,FAIL_PREFLIGHT='yes'),text=True,capture_output=True)
            self.assertNotEqual(0,failed.returncode);self.assertFalse((root/'calls').exists())
            result=subprocess.run(['bash','-eu','-c',script],env=env,text=True,capture_output=True)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual([['download','-E','prometheus'],['setup','logging-gate','06'],['setup','logging-gate','07']],
                             [json.loads(line) for line in (root/'calls').read_text().splitlines()])
            for ip in LAB.PREVIOUS_HOSTS:
                self.assertIn('192.168.122.2 registry.talkschool.cn',(root/(ip+'-hosts')).read_text())
                self.assertIn('127.0.0.1 localhost',(root/(ip+'-hosts')).read_text())
                self.assertFalse((root/(ip+'-before')).exists(),'the rejected registry address must not return during route cleanup')
            # Fresh bootstrap consumes exactly the same inputs before setup 90;
            # later product setup must retain the selected registry endpoint.
            fresh=raw[raw.index('if [[ ! -s "$KC" ]]'):raw.index('export KUBECONFIG="$KC"')]
            self.assertLess(fresh.index('logging_registry_preflight'),fresh.index('"$K" new'))
            self.assertLess(fresh.index('logging_platform_inputs'),fresh.index('"$K" setup "$CLUSTER" 90'))
            self.assertLess(fresh.index('download -E prometheus'),fresh.index('"$K" setup "$CLUSTER" 90'))
            self.assertIn('logging_registry_input',raw[raw.index('echo LOGGING_STAGE_BEGIN product-entrypoint'):])

    def test_logging_artifact_cli_accepts_new_frozen_digest_and_keeps_default_contract(self):
        import os
        import re
        import shutil
        raw=(ROOT/'tests/helpers/logging-artifact-gate.sh').read_text()
        legacy=dict(re.findall(r"'(brinnatt/[^'=]+)=([a-f0-9]{64})'",raw))
        publication={'status':'pass','failure_markers':0,'durable_rc':0,'runs':[{'workflow_conclusion':'success',
            'durable_rc':0,'failure_markers':0,'run_id':123,'run_url':'https://github.com/brinnatt/kubeauto-ext-images-dockerfile/actions/runs/123'}],
            'artifact_sha256':{'raw':'f'*64},'images':[]}
        for key in ARTIFACTS.inventory():
            publication['images'].append({'key':key,'dockerhub_ref':key,'talkedu_ref':'hub.talkedu.cn/kubeauto/'+key.removeprefix('brinnatt/'),
                'platforms':{'linux/amd64':'sha256:'+'b'*64,'linux/arm64':'sha256:'+'c'*64},'head':'d'*40,'fingerprint':'e'*64,'digest':'sha256:'+'a'*64})
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in ('roles/cluster-addon','tests/helpers','.venv/bin','bin'):(root/name).mkdir(parents=True)
            (root/'roles/cluster-addon/files').symlink_to(ROOT/'roles/cluster-addon/files',target_is_directory=True)
            (root/'tests/helpers/elasticsearch-artifacts.py').symlink_to(ROOT/'tests/helpers/elasticsearch-artifacts.py')
            import shlex
            interpreter=root/'.venv/bin/python'
            interpreter.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');interpreter.chmod(0o700)
            receipt=root/'receipt.json';receipt.write_text(json.dumps(publication))
            docker=root/'bin/docker'
            docker.write_text('#!'+sys.executable+'\nimport json,os,sys\nkey=sys.argv[3]\nlegacy='+repr(legacy)+'\n'
                'digest=legacy[key] if os.environ.get("LEGACY") else os.environ.get("DIGEST","a"*64)\n'
                'print(json.dumps(["hub.talkedu.cn/kubeauto/"+key.removeprefix("brinnatt/").rsplit(":",1)[0]+"@sha256:"+digest]))\n')
            docker.chmod(0o700)
            env=dict(os.environ,KUBEAUTO_BASE=str(root),PATH=str(root/'bin')+':'+os.environ['PATH'],ELASTICSEARCH_IMAGE_RECEIPT=str(receipt))
            def run(values):return subprocess.run(['bash',ROOT/'tests/helpers/logging-artifact-gate.sh'],env=values,text=True,capture_output=True,timeout=20)
            result=run(env);self.assertEqual(0,result.returncode,result.stderr);self.assertIn('LOGGING_FROZEN_ARTIFACT_VERIFIED',result.stdout)
            failed=run(dict(env,DIGEST='b'*64));self.assertNotEqual(0,failed.returncode);self.assertNotIn('LOGGING_ARTIFACT_GATE_PASS',failed.stdout)
            env.pop('ELASTICSEARCH_IMAGE_RECEIPT');result=run(dict(env,LEGACY='yes'))
            self.assertEqual(0,result.returncode,result.stderr);self.assertIn('LOGGING_ARTIFACT_GATE_PASS',result.stdout)
            self.assertNotIn('LOGGING_FROZEN_ARTIFACT_VERIFIED',result.stdout)

    def test_real_transcript_and_durable_fences_are_required_for_route_reuse(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);stem=root/'route';log=root/'route.log'
            rc=LAB.stream_command(['bash',ROOT/'tests/helpers/run-durable-gate.sh',stem,'ROUTE_EXIT',
                'bash','-c','printf "LOGGING_FULL_GATE_PASS\\n"'],log,os.environ.copy(),10)
            self.assertEqual(0,rc)
            receipt={'inputs':{'gate':'a'},'durable':str(stem),'log':str(log),'sha256':LAB.hashlib.sha256(log.read_bytes()).hexdigest()}
            LAB.verify_logging_route(receipt,{'gate':'a'})
            with self.assertRaisesRegex(ValueError,'inputs changed'):LAB.verify_logging_route(receipt,{'gate':'b'})
            log.write_text('LOGGING_FULL_GATE_PASS\n[FAIL] late cleanup\n')
            receipt['sha256']=LAB.hashlib.sha256(log.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'complete clean success'):LAB.verify_logging_route(receipt,{'gate':'a'})
            stem.with_suffix('.finalized').write_text('1')
            with self.assertRaisesRegex(ValueError,'durable fence'):LAB.verify_logging_route(receipt,{'gate':'a'})

    def test_existing_logging_cluster_cannot_be_claimed_or_wiped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'clusters/logging-gate').mkdir(parents=True)
            with patch.object(LAB,'BASE',root),patch.object(LAB,'STATE',root/'state'),patch.object(LAB,'baseline'),patch.object(LAB,'cleanup') as cleanup:
                with self.assertRaisesRegex(ValueError,'unrelated cluster directory'):LAB.logging()
                cleanup.assert_not_called()

    def test_cleanup_refuses_changed_cluster_before_destroy_and_matches_fixed_host_inventory(self):
        import re
        raw=(ROOT/'tests/helpers/logging-regression.sh').read_text()
        block=raw.split("<<'HOSTS'",1)[1].split('\nHOSTS',1)[0]
        self.assertEqual(set(LAB.PREVIOUS_HOSTS),set(re.findall(r'^192\.168\.[0-9]+\.[0-9]+',block,re.M)))
        self.assertIn('"$K" setup "$CLUSTER" 90',raw)
        self.assertIn('"$K" setup "$CLUSTER" 07',raw)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);state=root/'state';state.mkdir();(root/'clusters/logging-gate').mkdir(parents=True)
            record={'hosts':list(LAB.PREVIOUS_HOSTS),'hardware':{ip:{'product_uuid':ip} for ip in LAB.PREVIOUS_HOSTS},'cluster_uid':'bound'}
            (state/'logging-ownership.json').write_text(json.dumps(record))
            with (patch.object(LAB,'BASE',root),patch.object(LAB,'STATE',state),patch.object(LAB,'host_capacity',side_effect=lambda ip:{'product_uuid':ip}),
                  patch.object(LAB,'load_es',return_value=SimpleNamespace(API=lambda *args:SimpleNamespace(get=lambda *args:{'metadata':{'uid':'foreign'}}))),
                  patch.object(LAB,'product') as product,patch.object(LAB,'execute') as execute):
                with self.assertRaisesRegex(ValueError,'changed Kubernetes identity'):LAB.logging_cleanup()
                product.assert_not_called();execute.assert_not_called()


class FilesystemContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ('etc', 'var/lib', 'data'): (self.root/name).mkdir(parents=True)
        (self.root/'etc/machine-id').write_text('machine')
        (self.root/'etc/fstab').write_text('# original\n')
        self.request = {'id': str(uuid.uuid4()), 'uuid': str(uuid.uuid4()), 'machine_id': 'machine', 'size': MODULE.SIZE}
        self.mount = self.root/MODULE.MOUNT.lstrip('/')
        self.image = self.root/'var/lib/kubeauto-longhorn-elasticsearch-v1'/self.request['id']/'disk.img'
        self.active = False
        self.commands = []
        self.runner = patch('subprocess.run', side_effect=self.command).start(); self.addCleanup(patch.stopall)
        patch('shutil.disk_usage', return_value=SimpleNamespace(free=48*1024**3, total=32*1024**3)).start()

    def command(self, argv, **kwargs):
        self.commands.append(argv)
        data = ''; rc = 0
        if argv[0] == 'findmnt':
            rc = 0 if self.active else 1
            data = json.dumps({'filesystems': [{'target': str(self.mount), 'source': '/dev/loop46', 'fstype': 'ext4', 'uuid': self.request['uuid']}]})
        elif argv[0] == 'losetup':
            data = json.dumps({'loopdevices': [{'name': '/dev/loop46', 'back-file': str(self.image)}] if self.active else []})
        elif argv[0] == 'mount': self.active = True
        elif argv[0] == 'umount':
            self.active = False
            (self.mount/'longhorn-disk.cfg').unlink(missing_ok=True)
        elif argv[0] != 'mkfs.ext4': raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, rc, data, '')

    def call(self, action): return MODULE.host_filesystem(self.request, action, self.root)

    def test_backing_capacity_is_admitted_before_creating_any_owned_files(self):
        for free in (8, 16, 39):
            with self.subTest(free_gib=free), patch('shutil.disk_usage', return_value=SimpleNamespace(free=free*1024**3)):
                with self.assertRaisesRegex(ValueError, 'backing-filesystem headroom'): self.call('prepare')
                self.assertFalse(self.image.parent.exists())
                self.assertEqual([], self.commands)
        self.call('prepare')
        with patch('shutil.disk_usage', return_value=SimpleNamespace(free=28*1024**3, total=29*1024**3)):
            with self.assertRaisesRegex(ValueError, 'usable filesystem capacity'): self.call('check')

    def test_peak_three_replica_schedule_fits_with_one_storage_node_absent(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'BASE', ROOT), patch.object(LAB, 'STATE', Path(temporary)), patch.object(LAB, 'host', side_effect=['machine\n', '{}']):
            LAB.filesystem(LAB.HOSTS[0], 'prepare')
            request = json.loads((Path(temporary)/('filesystem-'+LAB.HOSTS[0]+'.json')).read_text())
            self.assertEqual(MODULE.SIZE, request['size'])
        spec = importlib.util.spec_from_file_location('es_longhorn_capacity', ROOT/'roles/longhorn/files/longhorn.py')
        longhorn = importlib.util.module_from_spec(spec); spec.loader.exec_module(longhorn)
        import yaml
        config = yaml.safe_load((ROOT/'roles/longhorn/defaults/main.yml').read_text())
        config.update(longhorn_version='1.12.1', longhorn_data_engines=['v1'])
        settings = longhorn.chart_values(config)['defaultSettings']
        self.assertEqual(100, settings['storageOverProvisioningPercentage'])
        reserved = settings['storageReservedPercentageForDefaultDisk']
        # Seven 6 GiB data volumes and three 2 GiB master volumes, each with
        # three storage replicas. This is a feasible N-1 scheduled-capacity
        # witness, not a claim that every PVC is physically full or benchmarked.
        placement = [(4, 0)]*3 + [(3, 3)]*3
        self.assertEqual(len(LAB.HOSTS)-1, len(placement))
        self.assertEqual((21, 9), tuple(map(sum, zip(*placement))))
        self.assertEqual(144, sum(data*6+master*2 for data, master in placement))
        usable = 30*(100-reserved)/100  # runtime requires >=30 GiB after mkfs
        self.assertTrue(all(data*6+master*2 <= usable for data, master in placement))
        # The old 24 GiB file fails even with zero filesystem overhead: each
        # disk holds at most three 6 GiB replicas, leaving only seven master
        # slots for the required nine. Do not reintroduce this by total alone.
        old_limit = 24*(100-reserved)/100
        self.assertLess(old_limit, 4*6)
        self.assertLess((old_limit-3*6)//2*len(LAB.HOSTS), 9)

    def test_prepare_check_native_identity_and_exact_cleanup_preserve_fstab(self):
        saved = self.call('prepare'); self.assertEqual('loop-test-fixture', saved['kind'])
        native = str(uuid.uuid4()); (self.mount/'longhorn-disk.cfg').write_text(json.dumps({'diskUUID': native}))
        self.assertEqual(native, self.call('disk-identity')['disk_uuid'])
        self.assertEqual(saved, self.call('check'))
        self.assertTrue(self.call('remove')['removed']); self.assertFalse(self.image.exists())
        self.assertEqual('# original\n', (self.root/'etc/fstab').read_text())
        self.assertEqual(1, sum(c[0] == 'mkfs.ext4' for c in self.commands))

    def test_unassociated_loop_json_requires_explicit_list_mode(self):
        self.call('prepare')
        original=self.command
        def native_semantics(argv,**kwargs):
            if argv[0]=='losetup' and '-j' in argv and '-l' not in argv:
                return subprocess.CompletedProcess(argv,0,'','')
            return original(argv,**kwargs)
        with patch('subprocess.run',side_effect=native_semantics):
            self.assertTrue(self.call('remove')['removed'])
        self.assertFalse(self.image.exists())

    def test_missing_data_parent_is_owned_and_removed_after_real_directory_creation(self):
        (self.root/'data').rmdir()
        self.call('prepare')
        self.assertTrue(self.mount.is_dir())
        self.call('remove')
        self.assertFalse((self.root/'data').exists())

    def test_created_parent_preserves_new_foreign_contents_and_preexisting_parent(self):
        (self.root/'data').rmdir();self.call('prepare')
        foreign=self.root/'data/foreign';foreign.write_text('preserve')
        self.call('remove');self.assertEqual('preserve',foreign.read_text())
        self.assertTrue((self.root/'data').is_dir())

    def test_created_parent_replacement_refuses_before_unmount_or_deletion(self):
        (self.root/'data').rmdir();self.call('prepare')
        original=self.root/'data';original.rename(self.root/'old-data');original.mkdir()
        before=list(self.commands)
        with self.assertRaisesRegex(ValueError,'parent directory changed'):self.call('remove')
        self.assertEqual(before,self.commands);self.assertTrue(self.image.exists())

    def test_unknown_parent_creation_response_never_adopts_or_deletes_directory(self):
        self.call('prepare')
        record=self.image.with_name('owner.json');row=json.loads(record.read_text());row['parent_intent']=True;record.write_text(json.dumps(row))
        before=list(self.commands)
        with self.assertRaisesRegex(ValueError,'unconfirmed'):self.call('remove')
        self.assertEqual(before,self.commands);self.assertTrue(self.image.exists())

    def test_never_created_intent_is_safe_noop_but_unknown_mount_is_rejected(self):
        self.assertTrue(self.call('remove')['never_created'])
        self.mount.mkdir()
        with self.assertRaisesRegex(ValueError, 'missing.*ownership'): self.call('remove')

    def test_foreign_fstab_or_receipt_prevents_first_mutation(self):
        (self.root/'etc/fstab').write_text('/foreign '+str(self.mount)+' ext4 defaults 0 0\n')
        with self.assertRaisesRegex(ValueError, 'foreign'): self.call('prepare')
        self.assertEqual([], self.commands)

    def test_machine_replacement_rejected_before_subprocess(self):
        self.request['machine_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'host or capacity'): self.call('prepare')
        self.assertEqual([], self.commands)

    def test_image_inode_replacement_blocks_cleanup(self):
        self.call('prepare')
        original = self.image.with_name('original'); self.image.rename(original)
        self.image.write_bytes(b'foreign')
        with self.assertRaisesRegex(ValueError, 'replaced'): self.call('remove')
        self.assertEqual(b'foreign', self.image.read_bytes()); self.assertTrue(original.exists())

    def test_product_receipt_blocks_host_release(self):
        self.call('prepare'); p = self.root/'etc/kubeauto'; p.mkdir()
        (p/'longhorn-disk.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'receipt remains'): self.call('remove')
        self.assertTrue(self.active); self.assertTrue(self.image.exists())

    def test_prepare_failure_before_image_creation_can_be_cleaned(self):
        with patch('os.open', side_effect=OSError('injected')):
            with self.assertRaises(OSError): self.call('prepare')
        # Directory without an ownership record is retained, not guessed owned.
        with self.assertRaisesRegex(ValueError, 'missing.*ownership'): self.call('remove')

    def test_fstab_change_keeps_unrelated_admin_entry(self):
        self.call('prepare')
        with (self.root/'etc/fstab').open('a') as stream: stream.write('/admin /srv ext4 defaults 0 0\n')
        self.call('remove')
        self.assertIn('/admin /srv', (self.root/'etc/fstab').read_text())


class IdentityOwnershipContracts(unittest.TestCase):
    @contextmanager
    def fixture(self, defect='none'):
        from contextlib import nullcontext
        from tests.unit.test_elasticsearch_delivery import es, configuration
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state/'namespaces.json').write_text(json.dumps({'es-standard': 'namespace-uid'}))
            c = configuration() | {'elasticsearch_namespace': 'es-standard', 'elasticsearch_name': 'search',
                                   'elasticsearch_data_count': 3, 'elasticsearch_kibana_enabled': True}
            claims = [{'metadata': {'name': 'pvc-'+str(i), 'uid': 'claim-'+str(i)}} for i in range(6)]
            labels = es.meta(c, 'search-identity')['labels']
            tree = {'f:data': {('f:'+key): {} for key in ('cluster_uuid', 'pvc_uids', 'topology', 'version_ceiling')},
                    'f:metadata': {'f:labels': {('f:'+key): {} for key in labels}}}
            ledger = {'apiVersion': 'v1', 'kind': 'ConfigMap',
                      'metadata': {'name': 'search-identity', 'namespace': 'es-standard', 'uid': 'ledger-uid', 'resourceVersion': '1',
                                   'labels': labels, 'managedFields': [{'manager': 'kubeauto-elasticsearch', 'operation': 'Apply',
                                                                     'fieldsType': 'FieldsV1', 'fieldsV1': copy.deepcopy(tree)}]},
                      'data': {'cluster_uuid': 'cluster-uuid', 'topology': 'standard', 'version_ceiling': '9.5.6',
                               'pvc_uids': json.dumps({p['metadata']['name']: p['metadata']['uid'] for p in claims}, sort_keys=True)}}
            calls = []
            def get(kind, name='', namespace=''):
                if kind == 'namespace': return {'metadata': {'uid': 'foreign' if defect == 'namespace' else 'namespace-uid'}}
                if kind == 'configmap': return copy.deepcopy(ledger)
                if kind == 'pvc': return {'items': copy.deepcopy(claims)}
                if kind == 'elasticsearch':
                    return {'metadata': {'generation': 1}, 'status': {'observedGeneration': 1, 'phase': 'Ready', 'health': 'green',
                            'availableNodes': es.member_count(c), 'conditions': [{'type': 'ReconciliationComplete', 'status': 'True'}]}}
                raise AssertionError(kind)
            def request(method, route, body=None):
                if route == '/': return {'cluster_uuid': 'foreign' if defect == 'cluster' else 'cluster-uuid', 'version': {'number': '9.5.6'}}
                if route.startswith('/_cluster/health'): return {'status': 'green', 'number_of_nodes': es.member_count(c)}
                if route == '/_nodes': return {'nodes': {str(i): {'version': '9.5.6'} for i in range(es.member_count(c))}}
                raise AssertionError(route)
            def run(client, *args, body=None, **kwargs):
                if 'get' in args:
                    self.assertIn('--show-managed-fields', args)
                    return json.dumps(ledger)
                if args[0] != 'apply': return ''  # Kibana's health wait.
                obj = json.loads(body)
                if obj['kind'] == 'List':
                    obj = obj['items'][0]
                    for entry in ledger['metadata']['managedFields']:
                        if entry['manager'] != client.field_manager and obj['data'] != ledger['data']:
                            raise ValueError('SSA conflict pvc_uids')
                    ledger['data'] = obj['data']
                    if not any(row['manager'] == client.field_manager for row in ledger['metadata']['managedFields']):
                        ledger['metadata']['managedFields'].append({'manager': client.field_manager, 'operation': 'Apply',
                            'fieldsType': 'FieldsV1', 'fieldsV1': copy.deepcopy(tree)})
                    return ''
                self.assertEqual({'name': 'search-identity', 'namespace': 'es-standard', 'uid': 'ledger-uid', 'resourceVersion': '1'}, obj['metadata'])
                self.assertEqual({'apiVersion', 'kind', 'metadata'}, set(obj))
                self.assertIn('--server-side', args)
                self.assertIn('--show-managed-fields', args)
                self.assertIn('--field-manager=kubeauto-elasticsearch-gate', args)
                self.assertNotIn('--force-conflicts', args)
                preview = copy.deepcopy(ledger)
                preview['metadata']['managedFields'] = [row for row in preview['metadata']['managedFields'] if row['manager'] != 'kubeauto-elasticsearch-gate']
                calls.append('dry-run' if '--dry-run=server' in args else 'write')
                if defect == 'dry-run': preview['data'].pop('pvc_uids')
                if '--dry-run=server' not in args: ledger.update(preview)
                elif defect == 'drift': ledger['metadata']['resourceVersion'] = '2'
                return json.dumps(preview)
            with (patch.object(LAB, 'STATE', state), patch.object(LAB, 'selected_config', return_value=c),
                  patch.object(LAB, 'load_es', return_value=es), patch.object(es.API, 'get', side_effect=get),
                  patch.object(es.API, 'run', autospec=True, side_effect=run),
                  patch.object(es, 'connection', side_effect=lambda *a: nullcontext(request))):
                yield c, claims, ledger, calls, es

    def test_real_health_reproduces_shared_ownership_and_updates_seventh_pvc_after_relinquishment(self):
        with self.fixture() as (c, claims, ledger, calls, es):
            es.health(c, LAB.api())  # Reproduce the original gate's same-value apply.
            self.assertEqual(2, len(ledger['metadata']['managedFields']))
            claims.append({'metadata': {'name': 'pvc-6', 'uid': 'claim-6'}})
            c['elasticsearch_data_count'] = 4
            with self.assertRaisesRegex(ValueError, 'SSA conflict'): LAB.product_health(c)
            original = copy.deepcopy(ledger['data'])
            LAB.release_identity_ownership('standard')
            self.assertEqual(['dry-run', 'write'], calls)
            self.assertEqual(original, ledger['data'])
            LAB.product_health(c)
            self.assertEqual(7, len(json.loads(ledger['data']['pvc_uids'])))
            self.assertEqual('ledger-uid', ledger['metadata']['uid'])
            self.assertEqual(['kubeauto-elasticsearch'], [row['manager'] for row in ledger['metadata']['managedFields']])
            LAB.release_identity_ownership('standard')  # No second write when already repaired.
            self.assertEqual(['dry-run', 'write'], calls)

    def test_recovery_refuses_foreign_exclusive_unknown_or_changed_inputs_before_write(self):
        for defect in ('namespace', 'labels', 'exclusive', 'unknown', 'cluster', 'pvc', 'drift', 'dry-run'):
            with self.subTest(defect=defect), self.fixture(defect) as (c, claims, ledger, calls, es):
                shared = copy.deepcopy(ledger['metadata']['managedFields'][0])
                shared['manager'] = 'kubeauto-elasticsearch-gate'
                ledger['metadata']['managedFields'].append(shared)
                if defect == 'labels': ledger['metadata']['labels']['foreign'] = 'owner'
                if defect == 'exclusive': ledger['metadata']['managedFields'][0]['fieldsV1']['f:data'].pop('f:pvc_uids')
                if defect == 'unknown': ledger['metadata']['managedFields'][1]['fieldsV1']['f:data']['f:foreign'] = {}
                if defect == 'pvc': claims[0]['metadata']['uid'] = 'replacement'
                with self.assertRaises(ValueError): LAB.release_identity_ownership('standard')
                self.assertNotIn('write', calls)


class LabBoundaryContracts(unittest.TestCase):
    def test_fixture_and_product_apply_use_distinct_field_managers(self):
        from tests.unit.test_elasticsearch_delivery import es
        obj={'apiVersion':'v1','kind':'Namespace','metadata':{'name':'es-standard','labels':LAB.LABELS}}
        with patch.object(LAB,'load_es',return_value=es),patch.object(es.API,'run',return_value='') as run:
            LAB.api().apply([obj])
            self.assertIn('--field-manager=kubeauto-elasticsearch-gate',run.call_args.args)
            es.API('customer-context').apply([obj])
            self.assertIn('--field-manager=kubeauto-elasticsearch',run.call_args.args)

    def test_namespace_recovery_requires_bound_uid_product_labels_and_atomic_preconditions(self):
        from tests.unit.test_elasticsearch_delivery import es
        for defect in ('none','replaced','unrecorded','foreign-owner','foreign-product'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);name='es-standard'
                obj={'metadata':{'uid':'original','resourceVersion':'7','labels':dict(es.LABELS)}}
                (state/'namespaces.json').write_text(json.dumps({name:'original'} if defect!='unrecorded' else {}))
                if defect=='replaced':obj['metadata']['uid']='replacement'
                if defect=='foreign-owner':obj['metadata']['labels']['kubeauto.io/test-owner']='foreign'
                if defect=='foreign-product':obj['metadata']['labels']['kubeauto.io/component']='foreign'
                calls=[]
                def run(*args,**kwargs):
                    self.assertEqual(('patch','namespace',name,'--type=json','--field-manager=kubeauto-elasticsearch-gate','-p'),args[:-1])
                    operations=json.loads(args[-1])
                    self.assertEqual([{'op':'test','path':'/metadata/uid','value':'original'},
                        {'op':'test','path':'/metadata/resourceVersion','value':'7'},
                        {'op':'add','path':'/metadata/labels/kubeauto.io~1test-owner','value':LAB.LABELS['kubeauto.io/test-owner']}],operations)
                    calls.append(args);obj['metadata']['labels'].update(LAB.LABELS)
                k=SimpleNamespace(get=lambda *args:obj,run=run,apply=Mock())
                with patch.object(LAB,'STATE',state),patch.object(LAB,'api',return_value=k),patch.object(LAB,'load_es',return_value=es):
                    if defect=='none':
                        LAB.scenario_namespace(name);LAB.scenario_namespace(name)
                        self.assertEqual(1,len(calls))
                        self.assertEqual('original',json.loads((state/'namespaces.json').read_text())[name])
                    else:
                        with self.assertRaisesRegex(ValueError,'foreign scenario namespace'):LAB.scenario_namespace(name)
                        self.assertEqual([],calls)
                    k.apply.assert_not_called()

    def test_storage_prepares_real_mtls_and_refuses_foreign_or_replaced_credentials_before_filesystems(self):
        from tests.unit.test_longhorn_delivery import product as longhorn
        for pool in ('source', 'dr'):
            with self.subTest(pool=pool), tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);objects={};created=[]
                def get(kind,name='',namespace=''):
                    return objects.get((kind,name))
                def run(*args,body=None,**kwargs):
                    self.assertEqual(('create','-f','-','-o','json'),args)
                    obj=json.loads(body);obj['metadata']['uid']=str(uuid.uuid4())
                    objects[(obj['kind'].lower(),obj['metadata']['name'])]=obj
                    created.append(obj['kind']);return json.dumps(obj)
                k=SimpleNamespace(get=get,run=run)
                with (patch.object(LAB,'BASE',ROOT),patch.object(LAB,'STATE',state),
                      patch.object(LAB,'api',return_value=k),patch.object(LAB,'baseline',return_value={'cluster_uid':pool}),
                      patch.object(LAB,'storage_inventory'),patch.object(LAB,'filesystem') as filesystem,
                      patch.object(LAB,'configuration'),patch.object(LAB,'product') as product):
                    config={'longhorn_grpc_tls_required':True,'longhorn_storage_classes':[],'longhorn_backup_target':''}
                    with self.assertRaisesRegex(ValueError,'missing Longhorn credential'):longhorn.check_secrets(config,k)
                    LAB.storage_credentials();longhorn.check_secrets(config,k)
                    receipt=(state/'storage-credentials.json').read_bytes()
                    LAB.storage_credentials()
                    self.assertEqual(['Namespace','Secret'],created)
                    self.assertEqual(receipt,(state/'storage-credentials.json').read_bytes())
                    secret=objects[('secret','longhorn-grpc-tls')]
                    secret['metadata']['uid']='replacement'
                    with self.assertRaisesRegex(ValueError,'changed or unowned Secret'):LAB.storage()
                    filesystem.assert_not_called();product.assert_not_called()
                    (state/'storage-credentials.json').unlink()
                    with self.assertRaisesRegex(ValueError,'changed or unowned Secret'):LAB.storage_credentials()
                    del objects[('secret','longhorn-grpc-tls')]
                    (state/'namespaces.json').unlink()
                    with self.assertRaisesRegex(ValueError,'unowned namespace'):LAB.storage_credentials()

    def test_product_uses_the_cli_wrapper_installed_by_the_fixed_sync(self):
        self.assertIn('install -m 0755', (ROOT/'tests/helpers/sync-kubeauto.sh').read_text())
        self.assertIn('/usr/local/bin/kubecli', (ROOT/'tests/helpers/sync-kubeauto.sh').read_text())
        with patch.object(LAB, 'execute') as execute:
            LAB.product('setup', LAB.NAME, '07', '--tags', 'elasticsearch')
        self.assertEqual(['kubecli', 'setup', LAB.NAME, '07', '--tags', 'elasticsearch'], execute.call_args.args[0])

    def test_source_admission_refuses_changed_empty_cluster_before_any_wipe(self):
        class API:
            def get(self, *args): return {'metadata': {'uid': 'another-cluster'}}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base/'clusters/k8s-dev').mkdir(parents=True)
            (base/'clusters/k8s-dev/kubectl.kubeconfig').write_text('private')
            with patch.object(LAB, 'BASE', base), patch.object(LAB, 'DIRECTORY', base/'clusters/elasticsearch-gate'), patch.object(LAB, 'STATE', base/'state'), patch.object(LAB, 'load_es', return_value=SimpleNamespace(API=lambda *args: API())), patch.object(LAB, 'execute') as execute:
                with self.assertRaisesRegex(ValueError, 'changed since'): LAB.admission()
                execute.assert_not_called()

    def test_host_transport_rejects_unlisted_targets_before_ssh(self):
        with patch.object(LAB, 'execute') as execute:
            for target in ('192.168.122.1', '192.168.47.129', '192.168.122.135'):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'non-source'): LAB.host(target, ['true'])
            execute.assert_not_called()

    def test_actual_source_and_dr_inventory_satisfies_native_storage_validation_before_install(self):
        from tests.unit.test_longhorn_delivery import configuration, product as longhorn
        from ansible.parsing.dataloader import DataLoader
        from ansible.inventory.manager import InventoryManager
        from ansible.vars.manager import VariableManager
        for target in ('source', 'dr'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);state=root/'state';state.mkdir();directory=root/'cluster'
                hosts,nodes=(DR.HOSTS,DR.NODES) if target=='dr' else (LAB.HOSTS,LAB.NODES)
                (state/'ownership.json').write_text(json.dumps({'id':'source-owner'}))
                (state/'baseline.json').write_text(json.dumps({'cluster_uid':'source-uid'}))
                config=configuration();events=[]
                def product(*args,**kwargs):
                    if args[0]=='new': directory.mkdir()
                    events.append(args)
                def configure(**kwargs):config.update(kwargs)
                k=SimpleNamespace(get=lambda *args:{'metadata':{'uid':'target-uid'},'provisioner':'driver.longhorn.io'},run=Mock())
                with (patch.object(LAB,'BASE',root),patch.object(LAB,'STATE',state),patch.object(LAB,'DIRECTORY',directory),
                      patch.object(LAB,'HOSTS',hosts),patch.object(LAB,'NODES',nodes),patch.object(LAB,'NAME','elasticsearch-dr' if target=='dr' else 'elasticsearch-gate'),
                      patch.object(LAB,'admission',return_value={}),patch.object(LAB,'execute'),patch.object(LAB,'baseline'),
                      patch.object(LAB,'product',side_effect=product),patch.object(LAB,'configuration',side_effect=configure),
                      patch.object(LAB,'bootstrap_platform'),patch.object(LAB,'api',return_value=k),
                      patch.object(LAB,'bind_namespace'),patch.object(DR,'SOURCE',state),patch.object(DR,'admission',return_value={})):
                    if target=='source':LAB.bootstrap()
                    else:
                        with patch.object(LAB,'storage'):DR.bootstrap(LAB)
                    before=(directory/'hosts').read_text()
                    loader=DataLoader()
                    def inventory():return InventoryManager(loader=loader,sources=[str(directory/'hosts')])
                    inv=inventory();config.update(longhorn_storage_nodes=list(hosts),longhorn_inventory_nodes=list(hosts),
                                               longhorn_client_nodes=[h.name for h in inv.get_hosts('kube_node')])
                    with self.assertRaisesRegex(ValueError,'kube_node client inventory'):longhorn.validate(config)
                    def filesystem(address,action):
                        inv=inventory();config['longhorn_client_nodes']=[h.name for h in inv.get_hosts('kube_node')]
                        longhorn.validate(config);events.append(('filesystem',address,action))
                    with patch.object(LAB,'storage_credentials'), patch.object(LAB,'filesystem',side_effect=filesystem):LAB.storage()
                    inv=inventory();variables=VariableManager(loader=loader,inventory=inv)
                    self.assertEqual(set(hosts),{h.name for h in inv.get_hosts('kube_node')})
                    for address,node in zip(hosts,nodes):self.assertEqual(node,variables.get_vars(host=inv.get_host(address))['k8s_nodename'])
                    updated=(directory/'hosts').read_text();LAB.storage_inventory()
                    self.assertEqual(updated,(directory/'hosts').read_text())
                    for group in ('etcd','kube_master'):
                        import re
                        pattern=r'\['+group+r'\]\n.*?(?=\[)'
                        self.assertEqual(re.search(pattern,before,re.S)[0],re.search(pattern,updated,re.S)[0])
                    (directory/'hosts').write_text(updated.replace(hosts[-1], '192.168.122.1'))
                    with patch.object(LAB,'filesystem') as files, self.assertRaisesRegex(ValueError,'identity changed'):LAB.storage()
                    files.assert_not_called()

    def test_storage_uses_owned_filesystems_and_the_customer_download_setup_path(self):
        calls = []
        with patch.object(LAB, 'storage_credentials'), patch.object(LAB, 'storage_inventory'), patch.object(LAB, 'bind_namespace') as bind, patch.object(LAB, 'baseline'), patch.object(LAB, 'configuration') as config, patch.object(LAB, 'filesystem', side_effect=lambda address, action: calls.append((address, action))), patch.object(LAB, 'product', side_effect=lambda *args, **kwargs: calls.append(args)), patch.object(LAB, 'api', return_value=SimpleNamespace(get=lambda *args: {'provisioner': 'driver.longhorn.io'})):
            LAB.storage()
        self.assertEqual([(a, 'prepare') for a in LAB.HOSTS], calls[:len(LAB.HOSTS)])
        self.assertEqual(('download', '-E', 'longhorn'), calls[len(LAB.HOSTS)])
        self.assertEqual(('setup', 'elasticsearch-gate', '09'), calls[len(LAB.HOSTS)+1])
        self.assertEqual(['v1'], config.call_args.kwargs['longhorn_data_engines'])
        self.assertEqual('/data/kubeauto-longhorn-elasticsearch-v1', config.call_args.kwargs['longhorn_data_path'])
        self.assertFalse(config.call_args.kwargs.get('longhorn_v2_nodes'))
        bind.assert_called_once_with('longhorn-system')

    def test_publication_cannot_claim_success_without_complete_provenance_and_inventory(self):
        for data in ({}, {'status': 'pass', 'durable_rc': 0, 'failure_markers': 0},
                     {'status': 'pass', 'durable_rc': 0, 'failure_markers': 0, 'runs': [], 'artifact_sha256': {'raw': 'a'*64}}):
            with self.subTest(data=data), self.assertRaises(ValueError): ARTIFACTS.records(data)
        self.assertIn('brinnatt/elasticsearch:9.5.6', ARTIFACTS.inventory())
        self.assertIn('brinnatt/pause:3.10', ARTIFACTS.inventory())
        self.assertIn('brinnatt/longhorn-engine:v1.12.0', ARTIFACTS.inventory())



class ClientContracts(unittest.TestCase):
    def test_cleanup_outcomes_exercise_real_client_process_exit_and_running_interruption(self):
        import time
        pause=time.sleep
        for outcome in ('normal','failed','interrupted'):
            with self.subTest(outcome=outcome):
                objects={};processes=[];transcript=self.state/(outcome+'.txt');pod_states=[]
                def apply(resources):
                    for resource in resources:objects[resource['kind'].lower()]=copy.deepcopy(resource)
                    script=objects['job']['spec']['template']['spec']['containers'][0]['command'][2]
                    with transcript.open('w') as log:
                        processes.append(subprocess.Popen([sys.executable,'-c',script],stdout=log,stderr=log))
                def get(kind,name='',namespace=''):
                    if kind=='pods':
                        rc=processes[0].poll()
                        state=({'waiting':{'reason':'ContainerCreating'}} if outcome=='interrupted' and not pod_states else
                               {'running':{'startedAt':'2026-11-05T00:00:00Z'}} if rc is None else {'terminated':{'exitCode':rc}})
                        pod_states.append(state)
                        return {'items':[{'metadata':{'labels':{'job-name':objects['job']['metadata']['name']}},
                                          'status':{'containerStatuses':[{'name':'client','state':state}]}}]}
                    value=copy.deepcopy(objects.get(kind))
                    if value and kind=='job':
                        rc=processes[0].poll()
                        value['status']=({'active':1} if rc is None else {'failed':1} if rc else {'conditions':[{'type':'Complete','status':'True'}]})
                    return value
                def run(*args,**kwargs):
                    if 'logs' in args:
                        if outcome=='interrupted' and (not pod_states or 'running' not in pod_states[-1]):
                            raise ValueError('client container is still ContainerCreating')
                        return transcript.read_text()
                    if 'delete' in args:
                        if args[3]=='job' and processes[0].poll() is None:
                            processes[0].terminate();processes[0].wait(timeout=5)
                        objects.pop(args[3]);return ''
                    raise AssertionError(args)
                api=SimpleNamespace(apply=apply,get=get,run=run)
                try:
                    with patch.object(LAB,'api',return_value=api),patch.object(LAB,'diagnostics'),patch.object(LAB.time,'sleep',side_effect=lambda _:pause(.01)):
                        if outcome=='normal':LAB.client_job('standard','cleanup-probe',cleanup_outcome=outcome)
                        else:
                            expected=LAB.CleanupProbeFailure if outcome=='failed' else InterruptedError
                            with self.assertRaises(expected):LAB.client_job('standard','cleanup-probe',cleanup_outcome=outcome)
                    self.assertEqual({},objects)
                    self.assertIsNotNone(processes[0].poll())
                    if outcome=='failed':self.assertEqual(73,processes[0].returncode)
                    if outcome=='interrupted':
                        self.assertIn('waiting',pod_states[0])
                        self.assertTrue(any('running' in state for state in pod_states))
                finally:
                    for process in processes:
                        if process.poll() is None:process.kill();process.wait(timeout=5)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.config = {'elasticsearch_namespace': 'es-standard', 'elasticsearch_name': 'search',
                       'elasticsearch_admin_secret': '', 'elasticsearch_http_tls_secret': ''}
        (self.state/'config-standard.json').write_text(json.dumps(self.config))
        self.patches = [patch.object(LAB, 'BASE', ROOT), patch.object(LAB, 'STATE', self.state),
                        patch.object(LAB, 'baseline'), patch.object(LAB, 'load_es', return_value=SimpleNamespace(LABELS={'kubeauto.io/component': 'elasticsearch'}))]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def test_job_declaration_binds_real_script_tls_references_and_scoped_identity(self):
        c = self.config | {'elasticsearch_admin_secret': 'admin-ref', 'elasticsearch_http_tls_secret': 'tls-ref'}
        cm, job = LAB.client_objects(c, 'business', 'a'*16)
        self.assertEqual((ROOT/'tests/helpers/elasticsearch-business.py').read_text(), cm['data']['run.py'])
        pod = job['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(0, job['spec']['backoffLimit'])
        volumes = {v['name']: v for v in pod['volumes']}
        self.assertEqual('admin-ref', volumes['identity']['secret']['secretName'])
        self.assertEqual('tls-ref', volumes['ca']['secret']['secretName'])
        self.assertEqual('ca.crt', volumes['ca']['secret']['items'][0]['key'])
        env = {v['name']: v['value'] for v in pod['containers'][0]['env']}
        self.assertEqual('/identity/username', env['ELASTICSEARCH_USERNAME_FILE'])
        self.assertEqual('/identity/password', env['ELASTICSEARCH_PASSWORD_FILE'])
        for token in ('..', 'b'*15, 'g'*16):
            with self.assertRaises(ValueError): LAB.client_objects(c, 'business', token)

    def run_job(self, *, fail=False, foreign=False, missing_marker=False, cleanup_fail=False):
        events, objects = [], {}
        class API:
            def get(inner, kind, name='', namespace=''):
                obj = copy.deepcopy(objects.get(kind))
                if obj and foreign and kind == 'configmap': obj['metadata']['labels'] = {}
                return obj
            def apply(inner, resources):
                for r in resources: objects[r['kind'].lower()] = copy.deepcopy(r)
                objects['job']['status'] = {'failed': 1} if fail else {'conditions': [{'type': 'Complete', 'status': 'True'}]}
                events.append('apply')
            def run(inner, *argv, **kwargs):
                if 'logs' in argv:
                    events.append('logs')
                    return '\n'.join(['ELASTICSEARCH_BUSINESS_SCOPED_CLEAN_PASS module=business'] + ['ELASTICSEARCH_CASE_PASS id=ES-'+str(i) for i in range(15, 20 if missing_marker else 21)])
                if 'delete' in argv:
                    events.append('delete-'+argv[3])
                    if cleanup_fail: raise ValueError('fixture cleanup failure')
                    objects.pop(argv[3]); return ''
                raise AssertionError(argv)
        def diagnostic(path): events.append('diagnostics')
        with patch.object(LAB, 'api', return_value=API()), patch.object(LAB, 'diagnostics', side_effect=diagnostic):
            try: LAB.client_job('standard', 'business')
            except ValueError as exc: return str(exc), events, objects
        return None, events, objects

    def test_complete_job_consumes_every_case_and_proves_object_cleanup(self):
        error, events, objects = self.run_job()
        self.assertIsNone(error); self.assertEqual({}, objects)
        self.assertLess(events.index('logs'), events.index('delete-job'))
        self.assertTrue(list(self.state.glob('es-client-*.log')))

    def test_failed_job_preserves_first_failure_before_cleanup(self):
        with redirect_stderr(StringIO()):
            error, events, objects = self.run_job(fail=True, cleanup_fail=True)
        self.assertIn('Job failed', error)
        self.assertLess(events.index('diagnostics'), events.index('delete-job'))
        self.assertTrue(objects)

    def test_foreign_second_object_prevents_either_delete(self):
        error, events, objects = self.run_job(foreign=True)
        self.assertIn('foreign client', error)
        self.assertFalse(any(e.startswith('delete-') for e in events))
        self.assertEqual(2, len(objects))

    def test_missing_retry_case_cannot_pass_with_other_success_markers(self):
        error, _, objects = self.run_job(missing_marker=True)
        self.assertIn('lacks complete', error); self.assertEqual({}, objects)


class TopologyQualificationContracts(unittest.TestCase):
    def test_idempotence_compares_identity_and_business_ledger_after_product_reentry(self):
        events = []
        with patch.object(LAB, 'verify_topology', side_effect=lambda t: events.append('topology') or {}), patch.object(LAB, 'data_ledger', side_effect=lambda *a, **k: events.append('ledger-create' if k.get('create') else 'ledger-verify')), patch.object(LAB, 'topology_state', side_effect=lambda t: events.append('identity') or {'uid': 'same'}), patch.object(LAB, 'product', side_effect=lambda *a, **k: events.append('product')):
            LAB.qualify_install('standard')
        self.assertEqual(['topology', 'ledger-create', 'identity', 'product', 'topology', 'ledger-verify', 'identity'], events)

    def test_idempotence_identity_change_fails_before_success_markers(self):
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with patch.object(LAB, 'verify_topology'), patch.object(LAB, 'data_ledger'), patch.object(LAB, 'topology_state', side_effect=[{'uid': 'old'}, {'uid': 'new'}]), patch.object(LAB, 'product'), redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, 'unexpectedly replaced'): LAB.qualify_install('compact')
        self.assertNotIn('ELASTICSEARCH_CASE_PASS', output.getvalue())

    def test_temporary_selected_config_survives_readonly_ledger_handoffs(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)), patch.object(LAB, 'configuration') as config:
            p = Path(temporary)/'config-compact.json'; p.write_text(json.dumps({'elasticsearch_smartcn_enabled': False}))
            LAB.selected_config('compact', elasticsearch_smartcn_enabled=True)
            self.assertTrue(LAB.selected_config('compact')['elasticsearch_smartcn_enabled'])
            self.assertTrue(config.call_args.kwargs['elasticsearch_smartcn_enabled'])

    def test_smartcn_second_run_uses_current_pin_and_restores_baseline_even_on_failure(self):
        c = {'elasticsearch_ver': '9.5.6'}
        events = []
        class Connection:
            def __enter__(self):
                def request(method, path, body=None):
                    if path == '/_nodes/plugins': return {'nodes': {'one': {'plugins': [{'name': 'analysis-smartcn', 'elasticsearch_version': '9.5.6'}]}}}
                    if path == '/_analyze': return {'tokens': [{'token': '中华'}, {'token': '人民共和国'}]}
                    raise AssertionError(path)
                return request
            def __exit__(self, *args): pass
        def selected(topology, **updates): events.append(('config', updates)); return c
        with patch.object(LAB, 'baseline'), patch.object(LAB, 'client_job'), patch.object(LAB, 'selected_config', side_effect=selected), patch.object(LAB, 'product', side_effect=lambda *a, **k: events.append(('product', a))), patch.object(LAB, 'verify_topology'), patch.object(LAB, 'data_ledger'), patch.object(LAB, 'load_es', return_value=SimpleNamespace(connection=lambda *a: Connection())), patch.object(LAB, 'api'), patch.object(LAB, 'topology_state', side_effect=[{'uid': 'before'}, {'uid': 'after'}]):
            with self.assertRaisesRegex(ValueError, 'reentry unexpectedly'): LAB.lifecycle()
        self.assertEqual(('config', {'elasticsearch_smartcn_enabled': False}), events[-2])
        self.assertEqual(('product', ('setup', LAB.NAME, '07', '--tags', 'elasticsearch')), events[-1])


class FaultRecoveryContracts(unittest.TestCase):
    def test_ondelete_recovery_resumes_operator_before_waiting_for_shutdown_nodes(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            objects = {
                'es-standard': {'metadata': {'uid': 'master-uid'}, 'spec': {'replicas': 1, 'updateStrategy': {'type': 'OnDelete'}}, 'status': {'readyReplicas': 1}},
                'elastic-system': {'metadata': {'uid': 'operator-uid'}, 'spec': {'replicas': 0}, 'status': {'readyReplicas': 0}},
            }
            for ns, name, count in (('es-standard', 'search-es-master', 3), ('elastic-system', 'elastic-operator', 1)):
                LAB.atomic(LAB.STATE/('fault-'+ns+'-'+name+'.json'), {'namespace': ns, 'name': name, 'kind': 'statefulset', 'uid': objects[ns]['metadata']['uid'], 'replicas': count})
            events = []
            def run(*args, **kwargs):
                ns = args[1]; events.append((ns, args[2]))
                if 'rollout' in args: raise ValueError('rollout status is only available for RollingUpdate strategy type')
                if 'scale' in args:
                    if ns == 'elastic-system': self.assertEqual(3, objects['es-standard']['spec']['replicas'])
                    objects[ns]['spec']['replicas'] = int(args[-1].split('=')[1])
                elif 'wait' in args:
                    if ns == 'es-standard' and objects['elastic-system']['status']['readyReplicas'] != 1:
                        raise ValueError('restarted ES nodes retain shutdown markers while Operator is paused')
                    self.assertIn('--for=jsonpath={.status.readyReplicas}='+str(objects[ns]['spec']['replicas']), args)
                    objects[ns]['status']['readyReplicas'] = objects[ns]['spec']['replicas']
                else: raise AssertionError(args)
            api = SimpleNamespace(get=lambda kind, name, ns: objects[ns], run=run)
            with patch.object(LAB, 'api', return_value=api): LAB.restore_faults()
            self.assertEqual([('es-standard','scale'),('elastic-system','scale'),('elastic-system','wait'),('es-standard','wait')], events)
            self.assertEqual([], list(LAB.STATE.glob('fault-*.json')))

    def test_operator_fault_requests_hidden_field_ownership_before_journaling(self):
        obj = {'metadata': {'uid': 'operator-uid', 'managedFields': [{'manager': 'kubeauto-eck-operator'}]},
               'spec': {'replicas': 1}}
        api = Mock()
        api.get.return_value = {'metadata': {'uid': 'operator-uid'}, 'spec': {'replicas': 1}}
        api.run.side_effect = lambda *args: json.dumps(obj if '--show-managed-fields=true' in args else api.get.return_value)
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)), patch.object(LAB, 'api', return_value=api):
            record = LAB.fault_record('statefulset', 'elastic-operator', 'elastic-system', 1)
            self.assertEqual('operator-uid', json.loads(record.read_text())['uid'])
            api.run.assert_called_once_with('-n', 'elastic-system', 'get', 'statefulset', 'elastic-operator', '--show-managed-fields=true', '-o', 'json')
            record.unlink()
            obj['metadata']['managedFields'] = [{'manager': 'foreign'}]
            with self.assertRaisesRegex(ValueError, 'foreign shared Operator'):
                LAB.fault_record('statefulset', 'elastic-operator', 'elastic-system', 1)
            self.assertFalse(record.exists())

    def test_safety_fault_journals_before_scaling_and_restores_both_topologies_on_client_failure(self):
        from contextlib import redirect_stdout
        events, output = [], StringIO()
        c = {'elasticsearch_namespace': 'es-standard', 'elasticsearch_name': 'search'}
        pod = {'metadata': {'name': 'search-es-data-0', 'uid': 'surviving-pod',
                'labels': {'elasticsearch.k8s.elastic.co/statefulset-name': 'search-es-master'}}}
        class API:
            def get(self, kind, name='', namespace=''):
                if kind == 'statefulset': return {'spec': {'replicas': 3}}
                if kind == 'pods': return {'items': [pod]}
                if kind == 'pod': return pod
                raise AssertionError(kind)
            def run(self, *args, **kwargs): events.append(('kubernetes', args))
        with patch.object(LAB, 'selected_config', return_value=c), patch.object(LAB, 'api', return_value=API()), patch.object(LAB, 'data_ledger', return_value={'index': 'es-gate-confirmed-'+'a'*32}), patch.object(LAB, 'fault_record', side_effect=lambda *a: events.append(('journal', a))), patch.object(LAB, 'client_job', side_effect=ValueError('first rejected-write failure')), patch.object(LAB, 'restore_faults', side_effect=lambda: events.append(('restore',))), patch.object(LAB, 'verify_topology') as verify, redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, 'first rejected-write'): LAB.unavailable_write_fault('standard', 'quorum')
        self.assertEqual(('journal', ('statefulset', 'elastic-operator', 'elastic-system', 1)), events[0])
        self.assertEqual(('journal', ('statefulset', 'search-es-master', 'es-standard', 3)), events[3])
        self.assertEqual(('restore',), events[-1])
        self.assertEqual(['standard', 'compact'], [c.args[0] for c in verify.call_args_list])
        self.assertNotIn('ELASTICSEARCH_CASE_PASS', output.getvalue())

    def test_fault_identity_is_journaled_before_injection_and_recovered_by_uid(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            obj = {'metadata': {'uid': 'operator-uid', 'managedFields': [{'manager': 'kubeauto-eck-operator'}]}, 'spec': {'replicas': 1}, 'status': {'readyReplicas': 1}}
            api = Mock(); api.get.return_value = obj
            api.run.return_value = json.dumps(obj)
            with patch.object(LAB, 'api', return_value=api):
                record = LAB.fault_record('statefulset', 'elastic-operator', 'elastic-system', 1)
                self.assertTrue(record.exists())
                api.run.assert_called_once_with('-n', 'elastic-system', 'get', 'statefulset', 'elastic-operator', '--show-managed-fields=true', '-o', 'json')
                api.run.reset_mock()
                obj['spec']['replicas'] = 0
                def run(*args, **kwargs):
                    if 'scale' in args: obj['spec']['replicas'] = 1
                api.run.side_effect = run
                LAB.restore_faults()
                self.assertFalse(record.exists())
                self.assertEqual(2, api.run.call_count)

    def test_recovery_refuses_replaced_controller_without_touching_it(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            obj = {'metadata': {'uid': 'operator-uid', 'managedFields': [{'manager': 'kubeauto-eck-operator'}]}, 'spec': {'replicas': 1}}
            api = Mock(); api.get.return_value = obj
            api.run.return_value = json.dumps(obj)
            with patch.object(LAB, 'api', return_value=api):
                record = LAB.fault_record('statefulset', 'elastic-operator', 'elastic-system', 1)
                api.run.reset_mock()
                obj['metadata']['uid'] = 'replacement'
                with self.assertRaisesRegex(ValueError, 'changed controller'): LAB.restore_faults()
                api.run.assert_not_called(); self.assertTrue(record.exists())

    def test_operator_interruption_always_recovers_before_claiming_pass(self):
        events = []
        api = Mock(); api.run.side_effect = lambda *a, **k: events.append('scale' if 'scale' in a else 'wait')
        with patch.object(LAB, 'api', return_value=api), patch.object(LAB, 'data_ledger'), patch.object(LAB, 'fault_record', side_effect=lambda *a: events.append('journal')), patch.object(LAB, 'client_job', side_effect=ValueError('first business failure')), patch.object(LAB, 'restore_faults', side_effect=lambda: events.append('restore')), patch.object(LAB, 'verify_topology'):
            with self.assertRaisesRegex(ValueError, 'first business failure'): LAB.operator_fault()
        self.assertEqual(['journal', 'scale', 'wait', 'restore'], events)


class WorkerMigrationContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.node = {'metadata': {'name': LAB.NODES[3], 'uid': 'original-node'}, 'spec': {},
                     'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        self.pod = {'metadata': {'name': 'search-es-data-0', 'namespace': 'es-standard', 'uid': 'original-pod',
                    'resourceVersion': '12', 'labels': {'elasticsearch.k8s.elastic.co/statefulset-name': 'search-es-data',
                        'elasticsearch.k8s.elastic.co/cluster-name': 'search'}},
                    'spec': {'nodeName': LAB.NODES[3]}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        self.binding = {'pvc_uid': 'original-pvc', 'pv_uid': 'original-pv', 'volume_uid': 'original-native-volume'}
        self.events, self.fenced = [], False
        self.api = SimpleNamespace(get=self.get, run=self.kubernetes_run)
        patches = [patch.object(LAB, 'STATE', self.state), patch.object(LAB, 'api', return_value=self.api),
                   patch.object(LAB, 'load_es'), patch.object(LAB, 'verify_topology'), patch.object(LAB, 'data_ledger'),
                   patch.object(LAB, 'cleanup_namespaces'), patch.object(LAB, 'client_job'),
                   patch.object(LAB, 'selected_config', return_value={'elasticsearch_namespace': 'es-standard', 'elasticsearch_name': 'search'}),
                   patch.object(LAB, 'host', return_value='{"machine_id":"fixture","boot_id":"fixture-boot"}'),
                   patch.object(LAB, 'volume_binding', side_effect=self.volume), patch.object(LAB, 'guest_fence', side_effect=self.fence)]
        for p in patches: p.start(); self.addCleanup(p.stop)

    def get(self, kind, name='', namespace=''):
        if kind == 'node': return copy.deepcopy(self.node)
        if kind == 'pods': return {'items': [copy.deepcopy(self.pod)]}
        if kind == 'pod': return copy.deepcopy(self.pod)
        raise AssertionError(kind)

    def kubernetes_run(self, *args, **kwargs):
        self.events.append(args[0])
        if args[0] == 'cordon':
            self.assertTrue(list(self.state.glob('fault-worker-*.json')))
            self.node['spec']['unschedulable'] = True
        elif args[0] == 'uncordon': self.node['spec']['unschedulable'] = False
        elif args[0] == 'delete':
            self.assertTrue(self.fenced)
            self.assertEqual('Unknown', self.node['status']['conditions'][0]['status'])
            self.assertEqual({'uid': 'original-pod', 'resourceVersion': '12'}, json.loads(kwargs['body'])['preconditions'])
            self.pod['metadata']['uid'] = 'replacement-pod'
            self.pod['spec']['nodeName'] = LAB.NODES[6]
        else: raise AssertionError(args)

    def fence(self, record, action):
        self.events.append(action)
        if action == 'inspect': return {'units': 'original-state'}
        if action == 'fence':
            journal = json.loads(next(self.state.glob('fault-worker-*.json')).read_text())
            self.assertTrue(journal['fence_intent']); self.assertTrue(journal['cordon_intent'])
            self.assertEqual('original-state', record['fence']['original']['units'])
            self.fenced = True; self.node['status']['conditions'][0]['status'] = 'Unknown'
        if action == 'restore':
            self.fenced = False; self.node['status']['conditions'][0]['status'] = 'True'
            return {'restored': True}
        return {'fenced': self.fenced, 'kubepods_pids': [], 'scope': 'guest-runtime'}

    def volume(self, pod, *, attached_node):
        self.events.append(('attachment', attached_node))
        return self.binding.copy()

    def test_worker_handoff_preserves_volume_and_recovers_services_and_scheduling_before_pass(self):
        from contextlib import redirect_stdout
        output = StringIO()
        with redirect_stdout(output): LAB.worker_fault()
        self.assertLess(self.events.index('fence'), self.events.index('probe'))
        self.assertLess(self.events.index('probe'), self.events.index('delete'))
        self.assertIn(('attachment', LAB.NODES[6]), self.events)
        self.assertLess(self.events.index('restore'), self.events.index('uncordon'))
        self.assertFalse(self.fenced); self.assertFalse(self.node['spec']['unschedulable'])
        self.assertEqual([], list(self.state.iterdir()))
        self.assertIn('ELASTICSEARCH_WORKER_SCOPED_CLEAN_PASS', output.getvalue())
        self.assertIn('"power_fence": false', output.getvalue())

    def test_changed_node_or_lost_positive_fence_refuses_force_delete_and_recovers(self):
        from contextlib import redirect_stdout
        for failure in ('ready', 'live-process'):
            self.setUp_fixture_node()
            original = self.fence
            def probe(record, action):
                result = original(record, action)
                if action == 'probe':
                    if failure == 'ready': self.node['status']['conditions'][0]['status'] = 'True'
                    else: result['kubepods_pids'] = [123]
                return result
            output = StringIO()
            with patch.object(LAB, 'guest_fence', side_effect=probe), redirect_stdout(output):
                with self.assertRaisesRegex(ValueError, 'must remain|no longer holds'): LAB.worker_fault()
            self.assertNotIn('delete', self.events); self.assertFalse(self.fenced)
            self.assertIn('uncordon', self.events); self.assertEqual([], list(self.state.iterdir()))
            self.assertNotIn('ELASTICSEARCH_CASE_PASS', output.getvalue())

    def setUp_fixture_node(self):
        self.events.clear(); self.node['spec'] = {}; self.node['status']['conditions'][0]['status'] = 'True'

    def test_changed_native_volume_is_a_failure_even_when_replacement_pod_is_ready(self):
        def changed(pod, *, attached_node):
            return dict(self.binding, volume_uid='foreign-volume') if attached_node == LAB.NODES[6] else self.binding.copy()
        with patch.object(LAB, 'volume_binding', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed the original PVC'): LAB.worker_fault()
        self.assertFalse(self.fenced); self.assertEqual([], list(self.state.iterdir()))

    def test_recovery_preserves_journal_when_original_node_or_service_state_cannot_be_restored(self):
        path = self.state/'fault-worker-fixture.json'
        LAB.atomic(path, {'node': LAB.NODES[3], 'node_uid': 'foreign-node', 'fence_intent': True})
        with self.assertRaisesRegex(ValueError, 'identity changed'): LAB.restore_worker(path)
        self.assertTrue(path.exists()); self.assertNotIn('restore', self.events)
        LAB.atomic(path, {'node': LAB.NODES[3], 'node_uid': 'original-node', 'fence_intent': True})
        with patch.object(LAB, 'guest_fence', return_value={'restored': False}):
            with self.assertRaisesRegex(ValueError, 'not restored'): LAB.restore_worker(path)
        self.assertTrue(path.exists()); self.assertNotIn('uncordon', self.events)


class CleanupNamespaceContracts(unittest.TestCase):
    def test_cleanup_refuses_foreign_or_replaced_namespace_before_destruction(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            (Path(temporary)/'namespaces.json').write_text(json.dumps({'longhorn-system': 'owned-uid'}))
            for name, uid in (('customer', 'external'), ('longhorn-system', 'replacement')):
                k = Mock(); k.get.return_value = {'items': [{'metadata': {'name': name, 'uid': uid}}]}
                with patch.object(LAB, 'api', return_value=k), self.subTest(name=name), self.assertRaisesRegex(ValueError, 'foreign or replaced'):
                    LAB.cleanup_namespaces()
                k.run.assert_not_called()

    def test_recorded_namespaces_and_platform_boundaries_allow_scoped_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            (Path(temporary)/'namespaces.json').write_text(json.dumps({'longhorn-system': 'owned-uid'}))
            k = Mock(); k.get.return_value = {'items': [{'metadata': {'name': name, 'uid': uid}} for name, uid in (('kube-system', 'platform'), ('longhorn-system', 'owned-uid'))]}
            with patch.object(LAB, 'api', return_value=k): LAB.cleanup_namespaces()


class MaintenanceContracts(unittest.TestCase):
    def test_completed_drain_reuse_refuses_replaced_volume_or_incomplete_receipt_without_new_eviction(self):
        for defect in ('none','volume','incomplete'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary)
                record={'phase':'incomplete' if defect=='incomplete' else 'complete','cluster_uid':'cluster',
                        'node':'worker','node_uid':'node-uid','pods':[{'name':'member','namespace':'es-standard','binding':{'volume_uid':'original'}}]}
                (state/'maintenance-drain.json').write_text(json.dumps(record))
                k=Mock()
                k.get.side_effect=lambda kind,*a:{'metadata':{'uid':'node-uid'},'spec':{}} if kind=='node' else {'spec':{'nodeName':'other-worker'}}
                with (patch.object(LAB,'STATE',state),patch.object(LAB,'api',return_value=k),patch.object(LAB,'baseline',return_value={'cluster_uid':'cluster'}),
                      patch.object(LAB,'verify_topology'),patch.object(LAB,'data_ledger'),patch.object(LAB,'cleanup_namespaces'),
                      patch.object(LAB,'volume_binding',return_value={'volume_uid':'replacement' if defect=='volume' else 'original'})):
                    if defect=='none':LAB.maintenance_drain()
                    else:
                        with self.assertRaises(ValueError):LAB.maintenance_drain()
                k.run.assert_not_called()

    def test_watermark_uses_pinned_ten_second_minimum_and_restores_rejected_write_boundary(self):
        from contextlib import nullcontext
        from tests.unit.test_elasticsearch_delivery import es, configuration
        import os
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary);requests=[];restored=[]
            c=configuration() | {'elasticsearch_name':'search','elasticsearch_namespace':'es-standard'}
            index='es-gate-confirmed-'+'a'*32
            ledger={'index':index,'index_uuid':'index-uid'}
            def request(method,route,body=None):
                if route.startswith('/_cluster/settings'):
                    if method=='PUT':
                        requests.append(body)
                        self.assertEqual('10s',body['persistent'][LAB.WATERMARK_KEYS[-1]])
                        levels=[int(body['persistent'][key][:-1]) for key in LAB.WATERMARK_KEYS[:3]]
                        self.assertGreater(levels[0],levels[1]);self.assertGreater(levels[1],levels[2]);self.assertGreater(levels[2],1000)
                        self.assertTrue(all(body['persistent'][key] is None for key in LAB.WATERMARK_KEYS[3:6]))
                    return {}
                if route=='/_nodes/stats/fs':return {'nodes':{'one':{'name':'search-es-data-0','fs':{'data':[{'available_in_bytes':1000}]}}}}
                if route.endswith('/_settings?flat_settings=true'):return {index:{'settings':{'index.blocks.read_only_allow_delete':'true'}}}
                raise AssertionError(route)
            def restore(path):
                record=json.loads(path.read_text());self.assertEqual('probe-rejected',record['rejected_id']);restored.append(record['topology']);path.unlink()
            k=SimpleNamespace(get=lambda *a:{'metadata':{'uid':'cr-uid'}})
            def client(topology,module,**kwargs):
                if module=='business':return ''
                config=c | {'probe_ip':'10.82.1.7','probe_fault':kwargs['fault_kind'],'probe_index':kwargs['fault_index']}
                _,job=LAB.client_objects(config,module,'a'*16)
                env={item['name']:item.get('value','') for item in job['spec']['template']['spec']['containers'][0]['env']}
                self.assertEqual('flood-watermark',env['ELASTICSEARCH_FAULT_KIND'])
                self.assertEqual('https://10.82.1.7:9200',env['ELASTICSEARCH_URL'])
                replies=[{index:{'settings':{'index.blocks.read_only_allow_delete':'true'}}},{'error':{'type':'cluster_block_exception'}}]
                with patch.dict(os.environ,env),patch.object(BUSINESS,'call',side_effect=replies) as call:
                    result=BUSINESS.unavailable_write(None,'probe')
                    self.assertEqual((429,),call.call_args.kwargs['expected'])
                return 'ELASTICSEARCH_BUSINESS_MEASUREMENT '+json.dumps(result)
            product=SimpleNamespace(connection=lambda *a:nullcontext(request),LABELS=es.LABELS)
            with (patch.object(LAB,'STATE',state),patch.object(LAB,'selected_config',return_value=c),patch.object(LAB,'api',return_value=k),
                  patch.object(LAB,'BASE',ROOT),patch.object(LAB,'load_es',return_value=product),
                  patch.object(LAB,'data_ledger',return_value=ledger),patch.object(LAB,'verify_topology'),
                  patch.object(LAB,'client_job',side_effect=client),
                  patch.object(LAB,'restore_watermarks',side_effect=restore)):
                LAB.maintenance_watermarks()
            self.assertEqual(2,len(requests));self.assertEqual(['standard','compact'],restored)
            self.assertFalse(list(state.glob('fault-*.json')))

    def test_completed_expansion_and_scale_reuse_verified_results_without_product_mutations(self):
        identity={'cr_uid':'original','pvc_uids':{'claim':'uid'},'credentials_uid':'secret','credentials_sha256':'sha'}
        for defect in ('none','scale-identity','expanded-capacity'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary)
                (state/'maintenance-scale.json').write_text(json.dumps({'phase':'complete','after':identity}))
                for topology in ('standard','compact'):
                    (state/('maintenance-expansion-'+topology+'.json')).write_text(json.dumps({'phase':'complete','identity':identity,'filesystems':{}}))
                current=dict(identity,cr_uid='replaced') if defect=='scale-identity' else identity
                c={'elasticsearch_data_pvc_size':'6Gi'}
                with (patch.object(LAB,'STATE',state),patch.object(LAB,'selected_config',return_value=c),patch.object(LAB,'api'),
                      patch.object(LAB,'load_es'),patch.object(LAB,'verify_topology'),patch.object(LAB,'data_ledger'),
                      patch.object(LAB,'topology_state',return_value=current),
                      patch.object(LAB,'expanded_volumes',return_value=defect!='expanded-capacity') as expanded,
                      patch.object(LAB,'product') as product,patch.object(LAB,'product_refusal') as refusal):
                    if defect=='none':LAB.maintenance_scale();LAB.maintenance_expansion();self.assertEqual(2,expanded.call_count)
                    elif defect=='scale-identity':
                        with self.assertRaisesRegex(ValueError,'identity differs'):LAB.maintenance_scale()
                    else:
                        with self.assertRaisesRegex(ValueError,'retained volumes'):LAB.maintenance_expansion()
                    product.assert_not_called();refusal.assert_not_called()

    def test_pvc_and_pv_status_alone_do_not_prove_native_and_filesystem_expansion(self):
        from tests.unit.test_elasticsearch_delivery import es
        identity = {'cr_uid': 'original-cr', 'pvc_uids': {'elasticsearch-data-search-es-data-0': 'original-pvc'}}
        before = {'identity': identity, 'filesystems': {'search-es-data-0': 4*1024**3}}
        claim = {'metadata': {'name': 'elasticsearch-data-search-es-data-0'}, 'spec': {'volumeName': 'pv', 'resources': {'requests': {'storage': '6Gi'}}}, 'status': {'capacity': {'storage': '6Gi'}}}
        pv = {'spec': {'capacity': {'storage': '6Gi'}, 'csi': {'volumeHandle': 'volume'}}}
        native = {'spec': {'size': str(6*1024**3)}, 'status': {}}
        def get(kind, *args, **kwargs):
            return {'items': [claim]} if kind == 'pvc' else pv if kind == 'pv' else native
        with patch.object(LAB, 'load_es', return_value=es), patch.object(LAB, 'selected_config', return_value={'elasticsearch_namespace': 'es-standard'}), patch.object(LAB, 'api', return_value=SimpleNamespace(get=get)), patch.object(LAB, 'topology_state', return_value=identity), patch.object(LAB, 'data_filesystems', return_value=before['filesystems']) as fs:
            self.assertFalse(LAB.expanded_volumes('standard', before, '6Gi'))
            fs.return_value = {'search-es-data-0': int(5.8*1024**3)}
            self.assertTrue(LAB.expanded_volumes('standard', before, '6Gi'))
            native['status']['expansionRequired'] = True
            self.assertFalse(LAB.expanded_volumes('standard', before, '6Gi'))
            native['status'].clear(); pv['spec']['capacity']['storage'] = '4Gi'
            self.assertFalse(LAB.expanded_volumes('standard', before, '6Gi'))

    def test_scale_refusal_failure_restores_four_member_configuration_without_signing_case(self):
        from contextlib import nullcontext, redirect_stdout
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            updates, output = [], StringIO()
            c = {'elasticsearch_data_nodes': list(LAB.NODES[3:]), 'elasticsearch_name': 'search'}
            identity = {'cr_uid': 'cr', 'pvc_uids': {'original': 'pvc'}}
            def selected(topology, **kwargs): updates.append(kwargs); return c
            request = Mock(return_value=[{'node': 'search-es-data-3', 'state': 'STARTED'}])
            with patch.object(LAB, 'selected_config', side_effect=selected), patch.object(LAB, 'api'), patch.object(LAB, 'load_es', return_value=SimpleNamespace(data_count=lambda c: 3, connection=lambda *a: nullcontext(request))), patch.object(LAB, 'topology_state', return_value=identity), patch.object(LAB, 'product'), patch.object(LAB, 'verify_topology', return_value=c), patch.object(LAB, 'data_ledger', return_value={'index': 'ledger'}), patch.object(LAB, 'product_refusal', side_effect=ValueError('wrong refusal cause')), redirect_stdout(output):
                with self.assertRaisesRegex(ValueError, 'wrong refusal'): LAB.maintenance_scale()
            self.assertEqual({'elasticsearch_data_count': 4}, updates[-1])
            self.assertNotIn('ELASTICSEARCH_CASE_PASS', output.getvalue())
            self.assertNotIn('phase', json.loads((Path(temporary)/'maintenance-scale.json').read_text()))

    def test_watermark_http_429_requires_actual_block_and_cluster_block_error(self):
        import os
        index = 'es-gate-confirmed-'+'a'*32
        settings = {index: {'settings': {'index.blocks.read_only_allow_delete': 'true'}}}
        env = {'ELASTICSEARCH_FAULT_INDEX': index, 'ELASTICSEARCH_FAULT_KIND': 'flood-watermark', 'ELASTICSEARCH_ASSERT_HOSTNAME': 'search-client.es-standard.svc'}
        for error in ('cluster_block_exception', 'security_exception'):
            with patch.dict(os.environ, env), patch.object(BUSINESS, 'call', side_effect=[settings, {'error': {'type': error}}]) as call:
                if error == 'cluster_block_exception':
                    result = BUSINESS.unavailable_write(None, 'owned'); self.assertEqual(429, result['write_http_status'])
                else:
                    with self.assertRaisesRegex(ValueError, 'safety boundary'): BUSINESS.unavailable_write(None, 'owned')
                self.assertEqual((429,), call.call_args.kwargs['expected'])

    def test_watermark_real_http_status_contract_rejects_permission_and_overload_errors(self):
        import os
        index='es-gate-confirmed-'+'a'*32
        env={'ELASTICSEARCH_FAULT_INDEX':index,'ELASTICSEARCH_FAULT_KIND':'flood-watermark','ELASTICSEARCH_ASSERT_HOSTNAME':'search-client.es-standard.svc'}
        for status,kind in ((429,'cluster_block_exception'),(403,'cluster_block_exception'),(429,'es_rejected_execution_exception'),(403,'security_exception')):
            with self.subTest(status=status,kind=kind),patch.dict(os.environ,env),patch.dict(sys.modules,{'elasticsearch':SimpleNamespace(ApiError=type('APIError',(Exception,),{}))}):
                client=Mock()
                client.perform_request.side_effect=[SimpleNamespace(meta=SimpleNamespace(status=200),body={index:{'settings':{'index.blocks.read_only_allow_delete':'true'}}}),
                    SimpleNamespace(meta=SimpleNamespace(status=status),body={'error':{'type':kind}})]
                if status==429 and kind=='cluster_block_exception':
                    self.assertEqual(429,BUSINESS.unavailable_write(client,'probe')['write_http_status'])
                else:
                    with self.assertRaises(ValueError):BUSINESS.unavailable_write(client,'probe')

    def test_watermark_recovery_restores_thresholds_and_checks_automatic_release_and_rejected_id(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            path = Path(temporary)/'fault-watermarks-standard.json'
            record = {'topology': 'standard', 'cr_uid': 'original-cr', 'index': 'ledger', 'index_uuid': 'index-id',
                      'rejected_id': 'unacknowledged', 'settings': {'persistent': {LAB.WATERMARK_KEYS[0]: None}, 'transient': {LAB.WATERMARK_KEYS[-1]: '30s'}}}
            events = []
            def request(method, route, body=None):
                events.append((method, route, body))
                if method == 'PUT': return {'acknowledged': True}
                if route.startswith('/_cluster'): return {'persistent': {}, 'transient': {LAB.WATERMARK_KEYS[-1]: '30s'}}
                if route.endswith('_mget'): return {'docs': [{'found': False}]}
                return {'ledger': {'settings': {'index.uuid': 'index-id'}}}
            k = Mock(); k.get.return_value = {'metadata': {'uid': 'original-cr'}}
            LAB.atomic(path, record)
            with patch.object(LAB, 'api', return_value=k), patch.object(LAB, 'selected_config', return_value={'elasticsearch_name': 'search', 'elasticsearch_namespace': 'es-standard'}), patch.object(LAB, 'load_es', return_value=SimpleNamespace(connection=lambda *a: nullcontext(request))), patch.object(LAB, 'data_ledger') as ledger:
                LAB.restore_watermarks(path)
            self.assertEqual(('PUT', '/_cluster/settings', record['settings']), events[0])
            ledger.assert_called_once_with('standard'); self.assertFalse(path.exists())


class IndependentRestoreContracts(unittest.TestCase):
    def test_target_reuses_native_fixture_contracts_with_disjoint_hosts_state_context_and_backend_cleanup(self):
        with patch.object(DR, 'BASE', ROOT): target = DR.target_lab()
        self.assertEqual(DR.HOSTS, target.HOSTS)
        self.assertFalse(set(target.HOSTS) & set(LAB.HOSTS))
        self.assertEqual('elasticsearch-dr', target.NAME)
        self.assertEqual(DR.SOURCE/'dr', target.STATE)
        self.assertEqual(ROOT/'clusters/elasticsearch-dr', target.DIRECTORY)
        self.assertEqual(LAB.HOSTS, tuple(LAB.PREVIOUS_HOSTS)+LAB.SPARE_HOSTS)
        target.s3_fixture('cleanup')
        with self.assertRaisesRegex(ValueError, 'cannot own'): target.s3_fixture('prepare')
        with self.assertRaisesRegex(ValueError, 'non-source host'): target.host(LAB.HOSTS[0], ['true'])

    def test_target_admission_rejects_cloned_vm_or_capacity_without_creating_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root/'source'; source.mkdir()
            (source/'ownership.json').write_text(json.dumps({'hosts':list(LAB.HOSTS), 'hardware':{'source':{'product_uuid':'source-vm'}}}))
            lab = SimpleNamespace(DIRECTORY=root/'absent', STATE=root/'state', execute=Mock(), host_capacity=Mock(), atomic=Mock())
            for defect in ('clone', 'capacity', 'healthy'):
                lab.host_capacity.side_effect = lambda address: {'product_uuid': 'source-vm' if defect=='clone' else address,
                                                                'free': (32 if defect=='capacity' else 80)*1024**3}
                with patch.object(DR,'SOURCE',source):
                    if defect=='healthy': self.assertEqual(3,len(DR.admission(lab)))
                    else:
                        with self.assertRaisesRegex(ValueError, 'overlaps|headroom'):DR.admission(lab)
            lab.atomic.assert_not_called()
            self.assertEqual(['--verify','--rocky-only',*DR.HOSTS],lab.execute.call_args.args[0][2:])

    def test_independent_restore_rejects_matching_source_uuid_and_checks_exact_business_hash_and_query(self):
        from contextlib import nullcontext
        documents = {str(i):{'business_id':str(i),'value':i*17} for i in range(32)}
        import hashlib
        record={'index':'ledger','source_cluster_uuid':'source-uuid','documents':documents,
                'sha256':hashlib.sha256(json.dumps(documents,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        target_uuid='target-uuid'
        def request(method,path,body=None):
            if path=='/':return {'cluster_uuid':target_uuid}
            if path.endswith('_mget'):return {'docs':[{'_id':key,'_source':value,'found':True} for key,value in documents.items()]}
            if path.endswith('_count'):return {'count':32}
            if '_search' in path:return {'_shards':{'failed':0},'hits':{'hits':[{'_source':documents['7']}]}}
            return {}
        lab=SimpleNamespace(selected_config=Mock(return_value={}),load_es=lambda:SimpleNamespace(connection=lambda *args:nullcontext(request)),api=Mock())
        self.assertEqual('target-uuid',DR.verify_documents(lab,record))
        target_uuid='source-uuid'
        with self.assertRaisesRegex(ValueError,'reused source'):DR.verify_documents(lab,record)
        target_uuid='target-uuid';record['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'content hash'):DR.verify_documents(lab,record)

    def test_file_realm_matches_pinned_pbkdf2_format_and_keeps_plaintext_out_of_hash_secret(self):
        k=Mock();k.get.return_value=None
        lab=SimpleNamespace(api=lambda:k,LABELS=LAB.LABELS,execute=Mock())
        DR.file_realm(lab,{'elasticsearch_namespace':'es-standard'})
        lab.execute.assert_not_called()
        objects=k.apply.call_args.args[0]
        password=objects[1]['stringData']['password']
        encoded=objects[0]['stringData']['users'].strip().split(':')[1]
        prefix,salt,derived=encoded.split('$')
        import base64,hashlib
        self.assertEqual('{PBKDF2}100000',prefix)
        self.assertEqual(32,len(base64.b64decode(salt)))
        self.assertEqual(base64.b64decode(derived),hashlib.pbkdf2_hmac('sha512',password.encode(),base64.b64decode(salt),100000,dklen=32))
        self.assertNotIn(password,objects[0]['stringData']['users'])


class DisasterHandoffContracts(unittest.TestCase):
    def setUp(self):
        from contextlib import nullcontext
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.target=self.root/'dr';self.target.mkdir()
        self.config={'elasticsearch_namespace':'es-compact','elasticsearch_name':'search'}
        (self.target/'config-target-compact.json').write_text(json.dumps(self.config))
        (self.root/'backup-compact.json').write_text(json.dumps({'index':'ledger'}))
        self.journal=self.root/'dr-disaster.json'
        self.journal.write_text(json.dumps({'phase':'target-serving','target_cluster_uuid':'target-uuid'}))
        self.fault=self.root/'fault-es-compact-search-es-data.json'
        self.fault.write_text(json.dumps({'kind':'statefulset','namespace':'es-compact','name':'search-es-data','uid':'source-sts','replicas':3}))
        self.events=[];self.identity='target-uuid';self.block=False;self.accept_block=True
        def request(method,path,body=None):
            self.events.append((method,path))
            if path=='/':return {'cluster_uuid':self.identity}
            if method=='PUT':self.block=self.accept_block;return {'acknowledged':True}
            return {'ledger':{'settings':{'index.blocks.write':str(self.block).lower()}}}
        self.request=Mock(side_effect=request)
        self.es=SimpleNamespace(connection=lambda *args:nullcontext(self.request))
        self.k=Mock()
        self.k.get.side_effect=lambda *args,**kwargs:{'metadata':{'uid':'source-sts'},'spec':{'replicas':3},'status':{'readyReplicas':3}}
        self.k.run.side_effect=lambda *args,**kwargs:self.events.append(('source',args))
        target=SimpleNamespace(STATE=self.target,api=Mock())
        for obj,name,value in ((DR,'SOURCE',self.root),(DR,'target_lab',Mock(return_value=target)),(DR,'baseline',Mock()),
                               (LAB,'STATE',self.root),(LAB,'api',Mock(return_value=self.k)),(LAB,'load_es',Mock(return_value=self.es))):
            patch.object(obj,name,value).start()
        patch.dict(sys.modules,{LAB.__name__:LAB}).start()
        patch.object(LAB.importlib.util,'spec_from_file_location',return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module:None))).start()
        patch.object(LAB.importlib.util,'module_from_spec',return_value=DR).start()
        self.addCleanup(patch.stopall)

    def test_real_fault_recovery_blocks_and_reads_back_target_before_restoring_any_source_controller(self):
        LAB.restore_faults()
        self.assertTrue(self.block);self.assertFalse(self.fault.exists());self.assertTrue(self.journal.exists())
        first_source=next(i for i,row in enumerate(self.events) if row[0]=='source')
        readback=self.events.index(('GET','/ledger/_settings?flat_settings=true'))
        self.assertLess(readback,first_source)

    def test_unproved_target_block_or_changed_identity_preserves_faults_and_never_scales_source(self):
        for defect in ('block','identity','unreachable'):
            with self.subTest(defect=defect):
                self.accept_block=defect!='block';self.identity='foreign' if defect=='identity' else 'target-uuid'
                original=self.request.side_effect
                if defect=='unreachable':self.request.side_effect=ValueError('target unreachable')
                with self.assertRaisesRegex(ValueError,'isolation|identity|unreachable'):LAB.restore_faults()
                self.k.run.assert_not_called();self.assertTrue(self.fault.exists());self.assertTrue(self.journal.exists())
                self.request.side_effect=original

    def test_route_checks_service_and_live_cluster_uuid_before_exposing_business_connection(self):
        route={'cluster':'elasticsearch-dr','namespace':'es-compact','service':'search-client','cluster_uuid':'target-uuid','source_isolated':True}
        for defect in ('service','uuid','healthy'):
            changed=dict(route)
            if defect=='service':changed['service']='foreign-client'
            if defect=='uuid':changed['cluster_uuid']='foreign'
            (self.root/'application-route.json').write_text(json.dumps(changed))
            if defect=='healthy':
                with DR.routed_connection(LAB) as request:self.assertIs(self.request,request)
            else:
                with self.assertRaisesRegex(ValueError,'service identity|different data cluster'):
                    with DR.routed_connection(LAB):self.fail('unverified route was exposed')


class DisasterVisibilityContracts(unittest.TestCase):
    def exercise(self, visibility_boundary, *, resume=False, defect='', phase='before-isolation'):
        import hashlib
        from contextlib import nullcontext, redirect_stdout
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target_state=root/'dr';target_state.mkdir()
            config={'elasticsearch_namespace':'es-compact','elasticsearch_name':'search'}
            documents={str(i):{'business_id':str(i),'value':i*17} for i in range(32)}
            backup={'index':'ledger','snapshot_uuid':'snapshot','source_cluster_uuid':'source-uuid','end_time_ms':1000,
                    'documents':documents,'sha256':hashlib.sha256(json.dumps(documents,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
            token='fixtureowner';tail_id='source-after-snapshot-'+token;target_id='target-after-cutover-'+token
            tail={'business_id':tail_id,'value':-11};events=[]
            source_model={'docs':copy.deepcopy(documents),'visible':copy.deepcopy(documents),'uuid':'source-uuid','blocked':False}
            target_model={'docs':copy.deepcopy(documents),'visible':copy.deepcopy(documents),'uuid':'target-uuid','blocked':False}
            ledger={'index':'ledger','index_uuid':'source-index','documents':copy.deepcopy(documents)}
            (root/'ownership.json').write_text(json.dumps({'id':'fixture-owner'}))
            (root/'backup-compact.json').write_text(json.dumps(backup))
            (target_state/'restore-compact.json').write_text(json.dumps({'source_snapshot_uuid':'snapshot'}))
            (target_state/'config-target-compact.json').write_text(json.dumps(config))
            if resume:
                source_model['docs'][tail_id]=tail;source_model['visible'][tail_id]=tail;ledger['documents'][tail_id]=tail
                target_model['docs']={};target_model['visible']={}
                journal={'phase':phase,'target_cluster_uuid':'target-uuid','source_cr_uid':'source-cr',
                    'snapshot_uuid':'snapshot','source_tail_id':tail_id,'source_tail':tail,
                    'target_write_id':target_id,'target_document':{'business_id':target_id,'value':-17},'source_snapshot_end_time':1}
                if phase=='source-isolating':journal.update(tail_ack_time=11,declared_failure_at=12)
                (root/'dr-disaster.json').write_text(json.dumps(journal))
                if defect=='target-uuid':target_model['uuid']='foreign'
                if defect=='target-data':target_model['docs']['foreign']={}
                if defect=='tail':source_model['docs'][tail_id]={'business_id':'foreign','value':9}
                if defect=='phase':
                    journal['phase']='target-serving';(root/'dr-disaster.json').write_text(json.dumps(journal))
                if defect=='fault':(root/'fault-stale.json').write_text('{}')
                if defect=='route':(root/'application-route.json').write_text('{}')
                if defect=='ledger':
                    source_model['docs']['7']={'business_id':'7','value':999};source_model['visible']['7']=source_model['docs']['7']
                    ledger['documents']['7']=source_model['docs']['7']
            (root/'ledger-compact.json').write_text(json.dumps(ledger))
            def requester(model):
                def request(method,path,body=None):
                    if path=='/':return {'cluster_uuid':model['uuid']}
                    if path.endswith('/_refresh'):
                        model['visible']=copy.deepcopy(model['docs']);return {'_shards':{'failed':0}}
                    if path.startswith('/_cat/indices'):return [{'index':name} for name in (['ledger'] if model['docs'] else [])]
                    if '_create/' in path or '/_doc/' in path:
                        identifier=path.split('/')[3].split('?')[0]
                        if method=='GET':return {'found':identifier in model['docs'],'_source':model['docs'].get(identifier),'_seq_no':17,'_primary_term':1}
                        if model['blocked']:raise ValueError('write blocked')
                        if '/_create/' in path and identifier in model['docs']:raise ValueError('create conflict')
                        if '/_doc/' in path:
                            self.assertIn('if_seq_no=17',path);self.assertIn('if_primary_term=1',path)
                        boundary=('target-write' if model is target_model else 'source-tail' if identifier==tail_id else 'failback')
                        model['docs'][identifier]=body;events.append(('write',boundary,path))
                        if boundary!=visibility_boundary or 'refresh=wait_for' in path:model['visible']=copy.deepcopy(model['docs'])
                        return {'_shards':{'successful':2,'failed':0}}
                    if path.endswith('/_mget'):return {'docs':[{'_id':key,'found':key in model['docs'],**({'_source':model['docs'][key]} if key in model['docs'] else {})} for key in body['ids']]}
                    if path.endswith('/_count'):return {'count':len(model['visible'])}
                    if '_search' in path:return {'_shards':{'failed':0},'hits':{'hits':[{'_source':model['visible']['7']}]}}
                    if '_settings' in path:
                        if method=='PUT':model['blocked']=body.get('index.blocks.write',False);events.append(('block',model['uuid']));return {'acknowledged':True}
                        settings=({'index.blocks.write':str(model['blocked']).lower()} if 'flat_settings' in path else
                                  {'index':{'uuid':'source-index' if model is source_model else 'target-index'}})
                        return {'ledger':{'settings':settings}}
                    raise AssertionError((method,path,body))
                return request
            source_api=SimpleNamespace(get=lambda kind,*args,**kwargs: {'metadata':{'uid':'foreign' if defect=='source-cr' else 'source-cr'}} if kind=='elasticsearch' else
                {'items':[{'metadata':{'labels':{'kubernetes.io/service-name':'search-client'}},'endpoints':None}]} if kind=='endpointslices' else {'items':[]},
                run=lambda *args,**kwargs:events.append(('source-control',args)))
            target_api=object();source_request=requester(source_model);target_request=requester(target_model)
            es=SimpleNamespace(connection=lambda c,k:nullcontext(source_request if k is source_api else target_request))
            def restore_source():
                self.assertTrue(target_model['blocked'] or not target_model['docs']);events.append(('source-restored',))
            source=SimpleNamespace(NAME='source',api=lambda:source_api,selected_config=lambda *args,**kwargs:config,
                load_es=lambda:es,atomic=LAB.atomic,data_ledger=lambda topology:LAB.data_ledger(topology) if topology=='compact' else {'documents':documents},verify_topology=Mock(),
                fault_record=Mock(),restore_faults=restore_source)
            if defect=='source-health':source.verify_topology.side_effect=ValueError('source unhealthy')
            target=SimpleNamespace(STATE=target_state,api=lambda:target_api,selected_config=lambda *args,**kwargs:config,
                load_es=lambda:es,atomic=LAB.atomic,verify_topology=Mock())
            if defect=='target-health':target.verify_topology.side_effect=ValueError('target unhealthy')
            def install(*args,**kwargs):target_model['docs']={};target_model['visible']={};events.append(('install',))
            def restore(*args,**kwargs):
                target_model['docs']=copy.deepcopy(documents);target_model['visible']=copy.deepcopy(documents)
                (target_state/'restore-compact.json').write_text(json.dumps({'target_cluster_uuid':target_model['uuid']}))
            with patch.object(DR,'SOURCE',root),patch.object(DR,'target_lab',return_value=target),patch.object(DR,'baseline'),\
                 patch.object(DR,'retire_business') as retire,patch.object(DR,'install',side_effect=install),patch.object(DR,'restore',side_effect=restore),\
                 patch.object(LAB,'STATE',root),patch.object(LAB,'api',return_value=source_api),patch.object(LAB,'selected_config',return_value=config),\
                 patch.object(LAB,'load_es',return_value=es),redirect_stdout(StringIO()):
                if defect:
                    with self.assertRaises(ValueError):DR.disaster(source)
                    self.assertFalse(events);source.fault_record.assert_not_called();retire.assert_not_called()
                else:
                    DR.disaster(source)
                    self.assertEqual('complete',json.loads((root/'dr-disaster.json').read_text())['phase'])
                    self.assertEqual(34,len(source_model['visible']));self.assertTrue(target_model['blocked'])
                    self.assertEqual(3,len([event for event in events if event[0]=='write']))
                    if resume:
                        retire.assert_not_called();self.assertNotIn(('install',),events)
                        archives=list((root/'dr-disaster-history').glob('*.json'));self.assertEqual(1,len(archives))
                        archived=json.loads(archives[0].read_text());self.assertEqual(journal,archived)
                        current=json.loads((root/'dr-disaster.json').read_text())
                        self.assertTrue(current['tail_reconfirmed_for_resume'])
                        if phase=='source-isolating':
                            self.assertGreater(current['tail_ack_time'],journal['tail_ack_time'])
                            self.assertGreater(current['declared_failure_at'],journal['declared_failure_at'])

    def test_actual_cutover_and_failback_wait_for_search_visibility_at_every_write_boundary(self):
        for boundary in ('source-tail','target-write','failback'):
            with self.subTest(boundary=boundary):self.exercise(boundary)

    def test_recovered_source_isolating_retry_reuses_only_healthy_empty_target(self):
        self.exercise('source-tail',resume=True,phase='source-isolating')
        for defect in ('target-uuid','target-data','source-cr','tail','phase','fault','route','ledger','source-health','target-health'):
            with self.subTest(defect=defect):self.exercise('source-tail',resume=True,phase='source-isolating',defect=defect)

    def test_before_isolation_retry_reuses_verified_empty_target_and_refuses_changed_state(self):
        self.exercise('source-tail',resume=True)
        for defect in ('target-uuid','target-data','source-cr','tail','phase','fault','route','ledger'):
            with self.subTest(defect=defect):self.exercise('source-tail',resume=True,defect=defect)


class FeatureRestoreHandoffContracts(unittest.TestCase):
    def test_real_feature_handoff_checks_file_realm_and_snapshot_before_restore_then_starts_kibana(self):
        from contextlib import nullcontext
        for defect in ('realm','uuid','running-kibana','healthy'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);target=root/'dr';target.mkdir()
                config={'elasticsearch_kibana_enabled':defect=='running-kibana','elasticsearch_file_realm_secret':'file',
                        'elasticsearch_admin_secret':'admin','elasticsearch_requests':[]}
                (target/'config-target-standard.json').write_text(json.dumps(config));(root/'s3.json').write_text('{}')
                events=[]
                def request(method,path,body=None):
                    events.append(('api',path))
                    if path=='/_security/_authenticate':return {'authentication_realm':{'type':'native' if defect=='realm' else 'file'},'roles':['superuser']}
                    return {'snapshots':[{'uuid':'foreign' if defect=='uuid' else 'snapshot-uuid'}]}
                def selected(topology,**updates):config.update(updates);return dict(config)
                def product(*args,**kwargs):events.append(('product',copy.deepcopy(config)))
                lab=SimpleNamespace(STATE=target,NAME='target',api=Mock(),atomic=LAB.atomic,repository_request=Mock(return_value={'type':'repository'}),
                    selected_config=selected,product=Mock(side_effect=product),verify_topology=Mock(),
                    load_es=lambda:SimpleNamespace(connection=lambda *args:nullcontext(request)))
                with patch.object(DR,'SOURCE',root),patch.object(DR,'verify_documents',side_effect=lambda *args:events.append(('hash','verified')) or 'target-uuid'):
                    if defect=='healthy':
                        actual,identity=DR.restore_features(lab,{'snapshot':'backup','snapshot_uuid':'snapshot-uuid','index':'ledger'})
                        self.assertEqual('target-uuid',identity);self.assertTrue(actual['elasticsearch_kibana_enabled'])
                        configurations=[row[1] for row in events if row[0]=='product']
                        self.assertEqual(3,len(configurations));self.assertFalse(configurations[1]['elasticsearch_kibana_enabled'])
                        self.assertEqual(['security','kibana'],configurations[1]['elasticsearch_requests'][0]['feature_states'])
                        self.assertLess(events.index(('api','/_security/_authenticate')),events.index(('product',configurations[1])))
                        self.assertLess(events.index(('hash','verified')),events.index(('product',configurations[2])))
                        lab.verify_topology.assert_called_once_with('compact')
                    else:
                        with self.assertRaisesRegex(ValueError,'stopped Kibana|file-realm|snapshot identity'):
                            DR.restore_features(lab,{'snapshot':'backup','snapshot_uuid':'snapshot-uuid','index':'ledger'})
                        self.assertLessEqual(lab.product.call_count,1);lab.verify_topology.assert_not_called()


class PlatformCoexistenceUpgradeContracts(unittest.TestCase):
    def test_retained_kibana_resize_preserves_controller_fields_and_uses_uid_rv_cas(self):
        for defect in ('none','foreign','memory','conflict'):
            with self.subTest(defect=defect):
                container={'name':'kibana','image':'pinned','env':[{'name':'controller-field','value':'keep'}],
                    'resources':{'requests':{'memory':'4Gi' if defect=='memory' else '2Gi','cpu':'200m'},
                        'limits':{'memory':'2Gi','cpu':'1'}}}
                obj={'metadata':{'uid':'own','resourceVersion':'7','labels':{'app.kubernetes.io/managed-by':'kubeauto',
                    'kubeauto.io/component':'foreign' if defect=='foreign' else 'logging-efk'}},
                    'spec':{'version':'9.5.1','count':2,'podTemplate':{'spec':{'containers':[container]}}}}
                original=copy.deepcopy(obj)
                def run(*args):
                    self.assertEqual(('-n','logging','patch','kibana','logging','--type=json'),args[:6])
                    changes=json.loads(args[-1]);self.assertEqual('own',changes[0]['value']);self.assertEqual('7',changes[1]['value'])
                    self.assertEqual([container],changes[2]['value'])
                    self.assertEqual('/spec/podTemplate/spec/containers/0/resources',changes[3]['path'])
                    resources=changes[3]['value'];self.assertEqual('200m',resources['requests']['cpu']);self.assertEqual('1',resources['limits']['cpu'])
                    self.assertEqual('1Gi',resources['requests']['memory'])
                    if defect=='conflict':raise ValueError('resourceVersion conflict')
                api=SimpleNamespace(get=lambda *args:obj,run=Mock(side_effect=run),apply=Mock())
                lab=SimpleNamespace(api=lambda:api)
                desired={'requests':{'memory':'1Gi'},'limits':{'memory':'2Gi'}}
                if defect=='none':DR.resize_upgrade_kibana(lab,desired)
                else:
                    with self.assertRaisesRegex(ValueError,'foreign|unrelated|conflict'):DR.resize_upgrade_kibana(lab,desired)
                if defect in ('foreign','memory'):api.run.assert_not_called()
                api.apply.assert_not_called();self.assertEqual(original,obj)

    def test_upgrade_source_continuation_requires_owned_healthy_old_generation(self):
        for defect in ('none','no-logging','version','operator','namespace','fault','unhealthy'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);(state/'namespaces.json').write_text(json.dumps({'logging':'logging-uid','es-compact':'business-uid'}))
                if defect=='fault':(state/'fault-retained.json').write_text('{}')
                def get(kind,name,namespace=''):
                    if kind=='namespace' and name=='logging' and defect=='no-logging':return None
                    if kind=='namespace':return {'metadata':{'uid':'foreign' if defect=='namespace' else 'logging-uid' if name=='logging' else 'business-uid'}}
                    return {'spec':{'template':{'spec':{'containers':[{'name':'manager','image':
                        'foreign' if defect=='operator' else 'registry.talkschool.cn:5000/brinnatt/eck-operator:3.4.1'}]}}}}
                lab=SimpleNamespace(STATE=state,api=lambda:SimpleNamespace(get=get),selected_config=Mock(return_value={
                    'elasticsearch_ver':'9.5.6' if defect=='version' else '9.5.5','elasticsearch_namespace':'es-compact'}),
                    cleanup_namespaces=Mock(),verify_topology=Mock(side_effect=ValueError('unhealthy') if defect=='unhealthy' else None),data_ledger=Mock())
                if defect in ('none','no-logging'):
                    self.assertTrue(DR.reuse_upgrade_source(lab));lab.data_ledger.assert_called_once_with('compact')
                else:
                    with self.assertRaisesRegex(ValueError,'pre-upgrade generation|unhealthy'):DR.reuse_upgrade_source(lab)
                    lab.data_ledger.assert_not_called()

    def test_platform_admission_uses_real_shared_guards_and_restores_pending_generation(self):
        from tests.unit.test_elasticsearch_delivery import ECKUpgradeAPI,eck
        for interrupted in (False,True):
            with self.subTest(interrupted=interrupted),tempfile.TemporaryDirectory() as temporary:
                upstream=ECKUpgradeAPI();products_written=[];journals={};refused=[]
                consumer={'kind':'Kibana','metadata':{'name':'logging','namespace':'logging','uid':'logging-uid',
                    'resourceVersion':'1','generation':1,'labels':{'app.kubernetes.io/managed-by':'kubeauto',
                    'kubeauto.io/component':'logging-efk'}},'spec':{'version':'9.5.1','count':2},
                    'status':{'health':'green','observedGeneration':1}}
                initial=copy.deepcopy(consumer);current={'selected':'original','eck_platform_upgrade_from':'','eck_platform_change_id':''};initial_config=dict(current)
                def get(kind,name='',namespace=''):
                    if kind=='kibana':return copy.deepcopy(consumer)
                    if kind=='configmap' and name in journals:return copy.deepcopy(journals[name])
                    value=copy.deepcopy(upstream.get(kind,name,namespace))
                    if kind=='crd':
                        for obj in value['items']:obj['metadata']['uid']='uid-'+obj['metadata']['name']
                    return value
                def apply(items,manager=None):
                    if manager is not None:products_written.append(items);raise AssertionError('platform guard failed before mutation')
                    for item in items:
                        item=copy.deepcopy(item);item['metadata'].update(uid='journal-uid',resourceVersion='7')
                        journals[item['metadata']['name']]=item
                def run(*args,body=None,**kwargs):
                    if args[0]=='delete':
                        self.assertEqual({'uid':'journal-uid','resourceVersion':'7'},json.loads(body)['preconditions'])
                        del journals[args[2].rsplit('/',1)[1]];return
                    self.assertEqual(('-n','logging','patch','kibana','logging','--type=json'),args[:6])
                    patch=json.loads(args[args.index('-p')+1])
                    for change in patch:
                        parent=consumer;parts=change['path'].strip('/').split('/')
                        for key in parts[:-1]:parent=parent[key]
                        if change['op']=='test':self.assertEqual(change['value'],parent[parts[-1]])
                        else:
                            if change['path']=='/spec/count' and parent[parts[-1]]!=change['value']:
                                consumer['metadata']['generation']+=1
                            parent[parts[-1]]=copy.deepcopy(change['value'])
                    consumer['metadata']['resourceVersion']=str(int(consumer['metadata']['resourceVersion'])+1)
                    if consumer['metadata'].get('annotations',{}).get('eck.k8s.elastic.co/managed')!='false':
                        consumer['status']['observedGeneration']=consumer['metadata']['generation']
                api=SimpleNamespace(get=get,apply=apply,run=run)
                def config(**updates):current.update(updates);return dict(current)
                def refusal(reason,label,**kwargs):
                    self.assertEqual({'tags':'eck-platform','marker':'ECK_SHARED_REFUSED'},kwargs)
                    self.assertEqual('3.4.1',current['eck_platform_upgrade_from'])
                    with self.assertRaisesRegex(ValueError,reason):
                        eck.ensure(api,'elastic-system',upstream.target,True,current['eck_platform_change_id'])
                    refused.append(label)
                    if interrupted and label=='platform-unhealthy':raise RuntimeError('after pending-generation refusal')
                lab=SimpleNamespace(STATE=Path(temporary),api=lambda:api,configuration=config,atomic=LAB.atomic,
                    product_refusal=refusal,topology_state=lambda _: {'pvc':'preserved'},
                    verify_topology=Mock(),data_ledger=Mock())
                rows=[{'kind':obj['kind'],'name':obj['metadata']['name'],'namespace':obj['metadata']['namespace'],
                       'uid':obj['metadata']['uid']} for obj in upstream.resources+[consumer]]
                with (patch.object(eck,'consumers',return_value=rows),patch.object(DR,'verify_upgrade_logging'),
                      redirect_stdout(StringIO()) as output):
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError,'after pending-generation refusal'):DR.upgrade_admission(lab,{})
                        self.assertNotIn('SUBCASE_PASS',output.getvalue())
                    else:
                        DR.upgrade_admission(lab,{})
                        self.assertIn('ES-50-platform-admission',output.getvalue())
                self.assertEqual(2 if interrupted else 3,len(refused));self.assertEqual([],products_written)
                self.assertEqual({},journals);self.assertEqual(initial_config,current)
                self.assertFalse((lab.STATE/'upgrade-admission.json').exists())
                self.assertEqual(initial['spec'],consumer['spec'])
                self.assertEqual(initial['metadata']['labels'],consumer['metadata']['labels'])
                self.assertEqual({},consumer['metadata']['annotations'])
                self.assertEqual(consumer['metadata']['generation'],consumer['status']['observedGeneration'])

    def test_platform_admission_recovery_refuses_replaced_or_unrelated_consumers(self):
        for defect in ('uid','labels','count','annotations'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                path=Path(temporary)/'upgrade-admission.json'
                record={'uid':'original','labels':{'owner':'fixture'},'annotations':{},'count':2}
                path.write_text(json.dumps(record));obj={'metadata':{'uid':'original','resourceVersion':'7',
                    'labels':dict(record['labels']),'annotations':{}},'spec':{'count':2}}
                if defect=='count':obj['spec']['count']=4
                elif defect=='uid':obj['metadata']['uid']='foreign'
                elif defect=='labels':obj['metadata']['labels']={'owner':'foreign'}
                else:obj['metadata']['annotations']={'foreign':'change'}
                api=SimpleNamespace(get=lambda *args:obj,run=Mock())
                with self.assertRaisesRegex(ValueError,'refuses'):DR.restore_upgrade_admission(SimpleNamespace(STATE=path.parent,api=lambda:api))
                api.run.assert_not_called();self.assertTrue(path.exists())

    def test_old_efk_template_qualifies_real_platform_guards_and_preserves_its_data_before_cleanup(self):
        from tests.unit.test_elasticsearch_delivery import configuration, es, eck
        import yaml
        from contextlib import nullcontext
        for fault in ('none','tight','reuse','capacity','credential-change','data-change'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);state=root/'state';state.mkdir()
                (root/'s3.json').write_text(json.dumps({'endpoint':'https://s3.example.com'}))
                c=yaml.safe_load((ROOT/'conf/config.yml').read_text()) | configuration('compact')
                objects={};applied=[];documents={};deleted=[]
                def apply(items):
                    for item in items:
                        obj=copy.deepcopy(item);m=obj['metadata'];m.update(uid='uid-'+m['name'],resourceVersion='7',generation=1)
                        if obj['kind']=='Secret':
                            import base64
                            obj['data']={key:base64.b64encode(value.encode()).decode() for key,value in obj['stringData'].items()}
                        if obj['kind'] in ('Elasticsearch','Kibana'):
                            obj['status']={'health':'green','observedGeneration':1,'availableNodes':3,'phase':'Ready'}
                        objects[obj['kind'].lower(),m['name'],m.get('namespace','')]=obj
                        applied.append(obj)
                def get(kind,name='',namespace=''):
                    if kind=='nodes':return {'items':[{'metadata':{'name':node},'status':{'allocatable':{
                        'memory':'8Gi' if fault=='capacity' else '13.5Gi' if fault=='tight' else '16Gi'}}} for node in DR.NODES]}
                    if kind=='pods':
                        pods=[{'spec':{'nodeName':node,'containers':[{'resources':{'requests':{'memory':'4Gi'}}}]}} for node in DR.NODES]
                        if fault=='reuse' and ('namespace','logging','') in objects:
                            pods += [{'metadata':{'namespace':'logging'},'spec':{'nodeName':node,
                                'containers':[{'resources':{'requests':{'memory':'8Gi'}}}]}} for node in DR.NODES]
                        return {'items':pods}
                    if kind=='pvc':return {'items':[{'metadata':{'name':'data-'+str(i),'uid':'claim-'+str(i)}} for i in range(3)]}
                    if kind=='pv':return {'items':[]}
                    return objects.get((kind,name,namespace))
                def run(*args,body=None,**kwargs):
                    if args[0]=='delete':
                        self.assertEqual('/api/v1/namespaces/logging',args[2])
                        self.assertEqual({'uid':'uid-logging','resourceVersion':'7'},json.loads(body)['preconditions'])
                        deleted.append(args[2]);objects.pop(('namespace','logging',''))
                api=SimpleNamespace(get=get,apply=apply,run=run)
                def request(method,path,body=None):
                    parts=path.strip('/').split('/')
                    if path=='/':return {'cluster_uuid':'efk-data','version':{'number':'9.5.1'}}
                    if parts[-1]=='_refresh':return {}
                    if parts[-1]=='_count':return {'count':1}
                    if len(parts)>1 and parts[1]=='_doc':return {'_source':documents[parts[0]]}
                    if len(parts)>1 and parts[1]=='_create':
                        documents[parts[0]]=body;return {'_shards':{'successful':2,'failed':0}}
                    return {'acknowledged':True}
                lab=SimpleNamespace(STATE=state,DIRECTORY=root,LABELS=LAB.LABELS,api=lambda:api,load_es=lambda:es,
                    configuration=lambda:dict(c),bind_namespace=lambda name:LAB.atomic(state/'namespaces.json',
                        {name:objects['namespace',name,'']['metadata']['uid']}),atomic=LAB.atomic)
                with patch.object(DR,'BASE',ROOT),patch.object(DR,'SOURCE',root), \
                        patch.object(es,'API',return_value=api), \
                        patch.object(DR,'upgrade_logging_connection',side_effect=lambda _:nullcontext(request)):
                    if fault=='capacity':
                        with self.assertRaisesRegex(ValueError,'capacity'):DR.upgrade_logging_fixture(lab)
                        self.assertEqual([],applied);continue
                    record=DR.upgrade_logging_fixture(lab)
                    elastic=[obj for obj in applied if obj['kind'] in ('Elasticsearch','Kibana')]
                    self.assertEqual(['Elasticsearch','Kibana'],[obj['kind'] for obj in elastic])
                    self.assertEqual('8Gi',elastic[0]['spec']['nodeSets'][0]['podTemplate']['spec']['containers'][0]['resources']['requests']['memory'])
                    self.assertEqual(2,elastic[1]['spec']['count'])
                    self.assertEqual({'requests':{'memory':'1Gi'},'limits':{'memory':'2Gi'}},
                        elastic[1]['spec']['podTemplate']['spec']['containers'][0]['resources'])
                    if fault=='reuse':
                        (state/'namespaces.json').write_text(json.dumps({'logging':'uid-logging'}))
                        credential=copy.deepcopy(objects['secret','logging-upgrade-s3','logging'])
                        self.assertEqual(record,DR.upgrade_logging_fixture(lab,retained=True))
                        self.assertEqual(credential,objects['secret','logging-upgrade-s3','logging'])
                        self.assertEqual(1,sum(obj['kind']=='Secret' for obj in applied))
                        self.assertEqual(1,len(documents))
                    rows=[{'kind':obj['kind'],'name':'logging','namespace':'logging','uid':obj['metadata']['uid']} for obj in elastic]
                    with patch.object(eck,'consumers',return_value=rows):
                        identities=eck.upgrade_consumers(api)
                        eck.wait_consumers(api,identities)
                    if fault=='credential-change':objects['secret','logging-upgrade-s3','logging']['data']['s3.client.default.secret_key']='changed'
                    if fault=='data-change':documents[record['index']]={'changed':True}
                    if fault in ('none','tight','reuse'):
                        DR.verify_upgrade_logging(lab,record)
                        DR.retire_upgrade_logging(lab,record)
                        self.assertEqual(['/api/v1/namespaces/logging'],deleted)
                        self.assertEqual({},json.loads((state/'namespaces.json').read_text()))
                        self.assertFalse((state/'upgrade-logging.json').exists())
                        self.assertEqual(record,json.loads(next(state.glob('retired-upgrade-logging-*.json')).read_text()))
                    else:
                        with self.assertRaisesRegex(ValueError,'changed EFK'):DR.retire_upgrade_logging(lab,record)
                        self.assertEqual([],deleted)


class RetiredUpgradeFixtureContracts(unittest.TestCase):
    def test_stale_fixture_recovery_removes_only_empty_owned_retry_and_archives_old_identity(self):
        for defect in ('none','absent','active','owner','foreign','storage','resource','discovery','conflict'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);record={'namespace':'logging','namespace_uid':'old-uid','document':{'token':'original'}}
                active=state/'upgrade-logging.json';active.write_text(json.dumps(record))
                owners={'logging':'foreign' if defect=='owner' else 'old-uid','es-compact':'business'}
                (state/'namespaces.json').write_text(json.dumps(owners))
                current=None if defect=='absent' else {'metadata':{'uid':'old-uid' if defect=='active' else 'empty-retry',
                    'resourceVersion':'7','labels':dict(LAB.LABELS,**{'kubeauto.io/component':'logging'}),
                    'managedFields':[{'manager':'foreign' if defect=='foreign' else 'kubeauto-elasticsearch-gate','operation':'Apply'}]}}
                deleted=[]
                kinds=['secrets','pods','persistentvolumeclaims','configmaps','serviceaccounts','widgets.example.com']
                if defect=='discovery':kinds.remove('pods')
                def get(kind,*args):
                    if kind=='namespace':return copy.deepcopy(current)
                    if kind=='pv':return {'items':[{'spec':{'claimRef':{'namespace':'logging'}}}] if defect=='storage' else []}
                    raise AssertionError(kind)
                def run(*args,body=None,**kwargs):
                    nonlocal current
                    if args[0]=='get':
                        self.assertIn('--show-managed-fields=true',args)
                        return json.dumps(current)
                    if args[0]=='api-resources':return '\n'.join(kinds)
                    if args[0]=='-n':
                        name={'configmaps':'kube-root-ca.crt','serviceaccounts':'default'}.get(args[3])
                        if args[3]=='widgets.example.com' and defect=='resource':name='foreign-widget'
                        return json.dumps({'items':[{'metadata':{'name':name}}] if name else []})
                    if args[0]=='delete':
                        self.assertEqual('/api/v1/namespaces/logging',args[2])
                        self.assertEqual({'uid':'empty-retry','resourceVersion':'7'},json.loads(body)['preconditions'])
                        if defect=='conflict':raise ValueError('concurrent namespace replacement')
                        deleted.append(args[2]);current=None
                    return ''
                api=SimpleNamespace(get=get,run=run)
                lab=SimpleNamespace(STATE=state,LABELS=LAB.LABELS,api=lambda:api,atomic=LAB.atomic)
                if defect in ('none','absent','active'):
                    DR.recover_retired_upgrade_logging(lab)
                    if defect=='active':
                        self.assertTrue(active.exists());self.assertEqual([],deleted)
                        self.assertEqual(owners,json.loads((state/'namespaces.json').read_text()))
                    else:
                        self.assertFalse(active.exists());self.assertIsNone(current)
                        self.assertEqual({'es-compact':'business'},json.loads((state/'namespaces.json').read_text()))
                        self.assertEqual(record,json.loads(next(state.glob('retired-upgrade-logging-*.json')).read_text()))
                else:
                    with self.assertRaises(ValueError):DR.recover_retired_upgrade_logging(lab)
                    self.assertEqual([],deleted);self.assertEqual(record,json.loads(active.read_text()))
                    self.assertEqual(owners,json.loads((state/'namespaces.json').read_text()))
                    self.assertEqual([],list(state.glob('retired-upgrade-logging-*.json')))


class PlatformUpgradeFocusContracts(unittest.TestCase):
    def test_platform_focus_stops_after_its_real_product_handoff_and_scoped_cleanup(self):
        for failure in (False, True):
            identities={key:'preserved' for key in ('cr_uid','pvc_uids','credentials_uid','credentials_sha256')}
            api=Mock()
            api.get.side_effect=lambda kind,*args: ({'metadata':{'uid':'operator-owner'}} if kind=='statefulset' else
                {'metadata':{'uid':'new-pod','ownerReferences':[{'uid':'operator-owner'}]},
                 'status':{'conditions':[{'type':'Ready','status':'True'}]}})
            target=SimpleNamespace(NAME='dr',api=lambda:api,load_es=Mock(),data_ledger=Mock(),
                topology_state=Mock(return_value=identities),selected_config=Mock(return_value={}),
                configuration=Mock(),product=Mock(side_effect=ValueError('platform failed') if failure else None),
                verify_topology=Mock())
            source=SimpleNamespace(verify_topology=Mock(),data_ledger=Mock())
            executor=Mock();executor.__enter__=Mock(return_value=executor);executor.__exit__=Mock(return_value=False)
            executor.submit.return_value.result.return_value='old-pod'
            output=StringIO()
            with (self.subTest(failure=failure),patch.object(DR,'target_lab',return_value=target),
                  patch.object(DR,'baseline'),patch.object(DR,'restore_upgrade_admission'),
                  patch.object(DR,'recover_retired_upgrade_logging'),
                  patch.object(DR,'reuse_upgrade_source',return_value=True),
                  patch.object(DR,'upgrade_logging_fixture',return_value={'owned':True}),
                  patch.object(DR,'upgrade_admission'),patch.object(DR,'verify_upgrade_logging'),
                  patch.object(DR,'retire_upgrade_logging') as retire,
                  patch('concurrent.futures.ThreadPoolExecutor',return_value=executor),redirect_stdout(output)):
                if failure:
                    with self.assertRaisesRegex(ValueError,'platform failed'):DR.upgrade(source,platform_only=True)
                    retire.assert_not_called();source.verify_topology.assert_not_called()
                    self.assertNotIn('ELASTICSEARCH_CASE_PASS',output.getvalue())
                else:
                    DR.upgrade(source,platform_only=True)
                    self.assertEqual([('setup','dr','07','--tags',tag) for tag in
                                      ('eck-platform','eck-platform','elasticsearch')],
                                     [call.args for call in target.product.call_args_list])
                    retire.assert_called_once_with(target,{'owned':True})
                    self.assertEqual([('standard',),('compact',)],[call.args for call in source.data_ledger.call_args_list])
                    self.assertIn('ELASTICSEARCH_CASE_PASS id=ES-50',output.getvalue())
                    self.assertIn('ELASTICSEARCH_PLATFORM_UPGRADE_SCOPED_CLEAN_PASS',output.getvalue())
                    self.assertNotIn('id=ES-51',output.getvalue());self.assertNotIn('id=ES-52',output.getvalue())

    def test_platform_focus_has_fixed_launch_dispatch_dependencies_and_exact_acceptance_scope(self):
        self.assertEqual({'ES-50'},EVIDENCE.SCOPES['upgrade-platform'])
        inputs=EVIDENCE.dependencies(ROOT,'upgrade-platform')
        self.assertIn('tests/helpers/elasticsearch-dr.py',inputs)
        self.assertIn('tests/helpers/elasticsearch-lab.py#upgrade_platform',inputs)
        self.assertIn('roles/cluster-addon/files/eck.py',inputs)
        for name in ('elasticsearch-gate.sh','elasticsearch-runner.sh'):
            self.assertIn('upgrade|upgrade-platform|',(ROOT/'tests/helpers'/name).read_text())
        with patch.object(LAB,'upgrade') as upgrade:
            LAB.upgrade_platform()
        upgrade.assert_called_once_with(platform_only=True)


class ConcurrentConfigurationContracts(unittest.TestCase):
    def test_two_soak_writers_preserve_independent_updates_and_private_complete_files(self):
        from concurrent.futures import ThreadPoolExecutor
        import yaml
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'DIRECTORY',Path(temporary)):
            path=Path(temporary)/'config.yml';path.write_text('initial: preserved\n')
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks=[pool.submit(LAB.configuration,**{'topology_'+str(i):i}) for i in range(20)]
                for task in tasks:task.result()
            data=yaml.safe_load(path.read_text())
            self.assertEqual(dict(initial='preserved',**{'topology_'+str(i):i for i in range(20)}),data)
            self.assertEqual(0o600,path.stat().st_mode&0o777)
            self.assertEqual([],list(Path(temporary).glob('*.tmp')))


class ObservabilityContracts(unittest.TestCase):
    def test_alert_rules_wait_for_both_replicas_first_evaluation_without_accepting_errors(self):
        required={'ElasticsearchClusterRed','ElasticsearchUnassignedShards','ElasticsearchMetricsUnavailable'}
        healthy=[{'name':name,'health':'ok'} for name in sorted(required)]
        for defect in ('healthy','missing','unknown','error','last-error'):
            rules=copy.deepcopy(healthy)
            if defect=='missing':rules.pop()
            elif defect=='unknown':rules[0]['health']='unknown'
            elif defect=='error':rules[0]['health']='err'
            elif defect=='last-error':rules[0]['lastError']='evaluation failed'
            with self.subTest(defect=defect),patch.object(LAB,'prometheus_read',return_value={'groups':[{'rules':rules}]}):
                self.assertEqual(defect=='healthy',LAB.prometheus_es_rules_ready('prom-a',required))
        observed=[];attempts={'prom-a':0,'prom-b':0}
        def read(pod,endpoint):
            self.assertEqual('rules',endpoint);observed.append(pod);attempts[pod]+=1
            rules=copy.deepcopy(healthy)
            if pod=='prom-b' and attempts[pod]==1:
                for rule in rules:rule['health']='unknown'
            return {'groups':[{'rules':rules}]}
        with patch.object(LAB,'prometheus_read',side_effect=read),patch.object(LAB.time,'sleep'):
            LAB.wait_prometheus(['prom-a','prom-b'],'actual-alert-rules',lambda pod:LAB.prometheus_es_rules_ready(pod,required))
        self.assertEqual(['prom-a','prom-b','prom-a','prom-b'],observed)
        for defect in ('missing','unknown','error'):
            rules=copy.deepcopy(healthy)
            if defect=='missing':rules.pop()
            else:rules[0]['health']='unknown' if defect=='unknown' else 'err'
            with self.subTest(timeout=defect),patch.object(LAB,'prometheus_read',return_value={'groups':[{'rules':rules}]}),\
                    patch.object(LAB.time,'monotonic',side_effect=[0,0,601]),patch.object(LAB.time,'sleep'):
                with self.assertRaisesRegex(ValueError,'actual-alert-rules'):
                    LAB.wait_prometheus(['prom-a','prom-b'],'actual-alert-rules',lambda pod:LAB.prometheus_es_rules_ready(pod,required))

    def test_health_readiness_requires_real_pinned_health_series_and_healthy_target(self):
        target={'labels':{'namespace':'es','service':'metrics'},'health':'up'}
        for defect in ('healthy','missing','zero','duplicate','target-down'):
            values=[] if defect=='missing' else [{'value':[1,'0' if defect=='zero' else '1']}]
            if defect=='duplicate':values*=2
            actual=copy.deepcopy(target)
            if defect=='target-down':actual['health']='down'
            def read(pod,endpoint,**kwargs):
                if endpoint=='targets':return {'activeTargets':[actual]}
                self.assertEqual('elasticsearch_cluster_health_status{namespace="es",service="metrics",color="green"}',kwargs['query'])
                return {'result':values}
            with self.subTest(defect=defect),patch.object(LAB,'prometheus_read',side_effect=read):
                self.assertEqual(defect=='healthy',LAB.prometheus_es_ready('prom','es','metrics'))

    def test_monitoring_setup_reuses_installed_storage_without_changing_its_configuration(self):
        import yaml
        from jinja2 import Environment
        class BoundaryReached(Exception):pass
        for failed_setup in (False,True):
            with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'DIRECTORY',Path(temporary)),patch.object(LAB,'STATE',Path(temporary)):
                config=Path(temporary)/'config.yml';config.write_text('longhorn_install: "yes"\n')
                calls=[]
                def execute(argv,**kwargs):
                    calls.append(argv)
                    if argv[1]=='download':return ''
                    self.assertEqual(['kubecli','setup',LAB.NAME,'07'],argv[:4])
                    self.assertEqual(4,len(argv))
                    overrides=yaml.safe_load(config.read_text())
                    self.assertEqual('no',overrides['longhorn_install'])
                    control=yaml.safe_load((ROOT/'roles/longhorn/tasks/control.yml').read_text())
                    task=next(row for row in control if row['name']=='Check or reconcile the Longhorn product')
                    self.assertFalse(Environment().compile_expression(task['when'])(**overrides))
                    playbook=yaml.safe_load((ROOT/'playbooks/09.longhorn.yml').read_text())
                    for play in playbook:
                        for role in play.get('roles',[]):
                            if role.get('longhorn_phase') in ('host','reconcile'):
                                conditions=role['when'];conditions=[conditions] if isinstance(conditions,str) else conditions
                                self.assertFalse(all(Environment().compile_expression(condition)(**overrides) for condition in conditions))
                    if failed_setup:raise BoundaryReached()
                    return ''
                def get(kind,name=None,**kwargs):
                    if kind=='namespace':
                        self.assertEqual('monitor',name);return {'metadata':{'uid':'monitor-uid'}}
                    self.assertEqual('prometheus',kind);raise BoundaryReached()
                with patch.object(LAB,'baseline') as baseline,patch.object(LAB,'stage_images'),patch.object(LAB,'execute',side_effect=execute),patch.object(LAB,'api',return_value=SimpleNamespace(get=get)):
                    with self.assertRaises(BoundaryReached):LAB.observability()
                baseline.assert_called_once()
                self.assertEqual('yes',yaml.safe_load(config.read_text())['longhorn_install'])
                self.assertEqual(3,len(calls))
                if not failed_setup:
                    self.assertEqual({'monitor':'monitor-uid'},json.loads((Path(temporary)/'namespaces.json').read_text()))
                    template=Environment().from_string((ROOT/'roles/cluster-addon/templates/local-storage/local-path-storage.yaml.j2').read_text())
                    objects=list(yaml.safe_load_all(template.render(local_path_provisioner_ver='v0.0.31',local_path_storage_class='local-path',local_path_provisioner_dir='/opt/local-path-provisioner')))
                    namespaces={row['metadata']['namespace'] for row in objects if 'namespace' in row.get('metadata',{})}
                    self.assertEqual({'kube-system'},namespaces)

    def test_all_actual_prometheus_replicas_must_converge_and_proxy_requests_name_the_pod(self):
        visited=[]
        def ready(pod):visited.append(pod);return len(visited)>2 or pod=='prom-a'
        with patch.object(LAB.time,'sleep'):
            LAB.wait_prometheus(['prom-a','prom-b'],'targets',ready)
        self.assertEqual(['prom-a','prom-b','prom-a','prom-b'],visited)
        k=Mock();k.run.return_value=json.dumps({'status':'success','data':{'result':[]}})
        with patch.object(LAB,'api',return_value=k):
            self.assertEqual({'result':[]},LAB.prometheus_read('prom-a','query',query='up{namespace="es-standard"}'))
        self.assertIn('/pods/http:prom-a:9090/proxy/api/v1/query?query=up%7B',k.run.call_args.args[2])

    def test_alert_cleanup_refuses_replaced_controller_or_index_before_delete_and_preserves_journal(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)):
            path=Path(temporary)/'fault-observability.json'
            record={'deployment_uid':'deployment','monitor_uid':'monitor','selector':{'matchLabels':{'app':'metrics'}},'index':'alert','index_uuid':'index'}
            path.write_text(json.dumps(record))
            c={'elasticsearch_namespace':'es-standard','elasticsearch_name':'search','elasticsearch_monitoring_namespace':'monitor'}
            k=Mock();request=Mock()
            def get(kind,*args):return {'metadata':{'uid':'foreign' if kind=='deployment' else 'monitor'}}
            k.get.side_effect=get
            with patch.object(LAB,'selected_config',return_value=c),patch.object(LAB,'api',return_value=k),patch.object(LAB,'load_es',
                    return_value=SimpleNamespace(connection=lambda *args:nullcontext(request))):
                with self.assertRaisesRegex(ValueError,'replaced resource'):LAB.restore_observability()
                k.run.assert_not_called();request.assert_not_called();self.assertTrue(path.exists())
                k.get.side_effect=lambda kind,*args:{'metadata':{'uid':'deployment' if kind=='deployment' else 'monitor'}}
                request.return_value={'alert':{'settings':{'index':{'uuid':'foreign'}}}}
                with self.assertRaisesRegex(ValueError,'unbound alert index'):LAB.restore_observability()
                self.assertEqual(['GET'],[call.args[0] for call in request.call_args_list]);k.run.assert_not_called();self.assertTrue(path.exists())


class S3NegativeContracts(unittest.TestCase):
    def test_no_delete_account_proves_authenticated_read_write_and_explicit_delete_refusal_before_ready(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(S3,'STATE',Path(temporary)):
            root=Path(temporary);record={'owner':'lease','access_key':'original','secret_key':'original-secret'}
            (root/'ownership.json').write_text(json.dumps({'id':'lease'}));(root/'s3.json').write_text(json.dumps(record))
            calls=[]
            def mc(client,observed,script,**kwargs):
                saved=json.loads((root/'s3.json').read_text())
                self.assertIn('negative',saved);self.assertNotEqual('ready',saved['negative'].get('phase'))
                calls.append((script,kwargs))
            with patch.object(S3,'matching_container',return_value=object()),patch.object(S3,'verify'),patch.object(S3,'mc',side_effect=mc):
                S3.negative(object(),'negative-prepare')
            saved=json.loads((root/'s3.json').read_text());account=saved['negative']
            self.assertEqual('ready',account['phase']);self.assertEqual(3,len(calls))
            policy=json.loads(calls[0][1]['extra']['POLICY'])
            self.assertIn({'Effect':'Deny','Action':['s3:DeleteObject'],'Resource':['arn:aws:s3:::elasticsearch-gate/*']},policy['Statement'])
            self.assertIn('mc cp',calls[1][0]);self.assertIn('mc cat',calls[1][0]);self.assertTrue(calls[2][1]['denied'])
            self.assertEqual(account['access_key'],calls[1][1]['credentials']['access_key'])
            self.assertEqual(calls[1][1]['credentials'],calls[2][1]['credentials'])

    def test_negative_recovery_checks_both_identities_before_applying_trust_or_secret_and_keeps_journal_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)):
            root=Path(temporary);path=root/'fault-s3-negative.json'
            record={'secret':{'uid':'secret','data':{'key':'original'}},'ca':{'uid':'ca','data':{'ca.crt':'original'}},'requests':[],'cluster_uuid':'cluster'}
            path.write_text(json.dumps(record))
            c={'elasticsearch_namespace':'es-standard','elasticsearch_s3_secret':'credentials','elasticsearch_s3_ca_configmap':'ca'}
            k=Mock();k.get.side_effect=lambda kind,*args:{'metadata':{'uid':'secret' if kind=='secret' else 'foreign'}}
            with (patch.object(LAB,'selected_config',return_value=c),patch.object(LAB,'api',return_value=k),patch.object(LAB,'product') as product,
                    patch.object(LAB,'s3_fixture') as fixture):
                with self.assertRaisesRegex(ValueError,'replaced trust or credential'):LAB.restore_s3_negative()
                k.apply.assert_not_called();k.run.assert_not_called();product.assert_not_called();fixture.assert_not_called();self.assertTrue(path.exists())


class UpgradeFixtureContracts(unittest.TestCase):
    def test_previous_operator_reuses_only_the_owned_pinned_empty_341_fixture(self):
        from tests.unit.test_elasticsearch_delivery import load as load_product
        for defect in ('none','foreign-owner','schema','watch','unexpected-version'):
            with self.subTest(defect=defect):
                eck=load_product('eck');eck.VERSION='3.4.1';eck.ROOT=ROOT/'tests/fixtures/elasticsearch'
                eck.PINS={'crds':'488f2eda4b0d21637d0a2764f0fced6eafbb7279d1a1fb9c35a9ebaac28e393a',
                          'operator':'acb8a565a3ad932e10091da32f9d5652de390ade105e8b6028314d3140576b8a'}
                manifests=eck.manifests();crds=manifests['crds']
                objects={row['kind']:row for row in manifests['operator']}
                controller=objects['StatefulSet'];controller['metadata'].update(uid='owned-controller',labels=eck.LABELS)
                controller['spec']['template']['spec']['containers'][0]['image']='registry.talkschool.cn:5000/brinnatt/eck-operator:3.4.1'
                for crd in crds:crd['metadata']['managedFields']=[{'manager':'kubeauto-eck-crd'}]
                if defect=='foreign-owner':controller['metadata']['labels']={}
                if defect=='schema':crds[0]['spec']['versions'][0]['served']=False
                if defect=='watch':objects['ConfigMap']['data']['eck.yaml']='restrict-watched-resources: true\n'
                if defect=='unexpected-version':controller['spec']['template']['spec']['containers'][0]['image']='unknown:3.3.0'
                def get(kind,name=None,namespace=None):
                    if kind=='crd':return {'items':crds}
                    if kind=='deployments,statefulsets':return {'items':[controller]}
                    if kind=='statefulset':return controller
                    if kind=='configmap':return objects['ConfigMap']
                    if kind=='namespace':return {'metadata':{'labels':eck.LABELS}}
                    return {'items':[]}
                api=Mock();api.get.side_effect=get
                lab=SimpleNamespace(DIRECTORY=Path('/fixture'),STATE=Path('/unmutated'),product=Mock(),bind_namespace=Mock())
                with (patch.object(DR,'BASE',ROOT),patch.object(DR.importlib.util,'spec_from_file_location',return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m:None))),
                      patch.object(DR.importlib.util,'module_from_spec',return_value=eck),patch.object(eck,'API',return_value=api)):
                    if defect=='none':DR.previous_eck(lab)
                    else:
                        with self.assertRaises(ValueError):DR.previous_eck(lab)
                api.apply.assert_not_called();lab.product.assert_not_called()
                if defect=='none':
                    self.assertEqual(1,api.run.call_count);self.assertIn('rollout',api.run.call_args.args)
                    lab.bind_namespace.assert_called_once_with('elastic-system')
                else:api.run.assert_not_called();lab.bind_namespace.assert_not_called()

    def test_upgrade_retirement_drops_only_its_obsolete_ledger_after_native_cleanup(self):
        for defect in ('none','foreign-namespace','product-failure','retained-pvs'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary);ledger=state/'ledger-compact.json';ledger.write_text('{"index_uuid":"old"}')
                protected=state/'ledger-standard.json';protected.write_text('protected')
                (state/'namespaces.json').write_text(json.dumps({'es-compact':'owned'}))
                config={'elasticsearch_name':'search','elasticsearch_namespace':'es-compact','elasticsearch_ver':'9.5.5'}
                (state/'config-target-compact.json').write_text(json.dumps(config))
                (state/'config-compact.json').write_text(json.dumps(config))
                deleted=False
                def get(kind,name=None,namespace=None):
                    if kind=='pv':return {'items':[{'metadata':{'uid':'retained'}}] if defect=='retained-pvs' else []}
                    if kind=='elasticsearch':return {'spec':{'version':'9.5.5'}}
                    return None if deleted else {'metadata':{'uid':'foreign' if defect=='foreign-namespace' else 'owned'}}
                def run(*args,**kwargs):
                    nonlocal deleted
                    self.assertTrue(ledger.exists())
                    deleted=True
                k=SimpleNamespace(get=get,run=Mock(side_effect=run))
                lab=SimpleNamespace(STATE=state,NAME='target',api=lambda:k,cleanup_namespaces=Mock(),
                                    configuration=Mock(),product=Mock(),atomic=lambda p,r:p.write_text(json.dumps(r)))
                if defect=='product-failure':lab.product.side_effect=ValueError('product failed')
                with patch.object(DR.time,'monotonic',side_effect=[0,601]):
                    if defect=='none':DR.retire_upgrade_fixture(lab,'compact')
                    else:
                        with self.assertRaises(ValueError):DR.retire_upgrade_fixture(lab,'compact')
                self.assertEqual(defect!='none',ledger.exists())
                self.assertEqual('protected',protected.read_text())
                if defect=='foreign-namespace':lab.product.assert_not_called();k.run.assert_not_called()

    def test_upgrade_retirement_hands_the_current_version_to_native_uninstall(self):
        from tests.unit.test_elasticsearch_delivery import BusinessAPI,es
        for version in ('9.5.5','9.5.6','9.6.0'):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary)
                product_api=BusinessAPI();product_api.cluster['spec']['version']=version
                original=dict(product_api.config,elasticsearch_ver='9.5.5')
                active=dict(original,elasticsearch_ver=version)
                path=state/'config-target-compact.json';path.write_text(json.dumps(original))
                (state/'config-compact.json').write_text(json.dumps(active))
                ledger=state/'ledger-compact.json';ledger.write_text('protected')
                k=Mock();k.get.return_value={'spec':{'version':version}}
                lab=SimpleNamespace(STATE=state,api=lambda:k,atomic=lambda p,r:p.write_text(json.dumps(r)))
                def retire(target,topology):
                    handed=json.loads(path.read_text())
                    self.assertEqual(active,handed)
                    self.assertTrue(ledger.exists())
                    es.preflight(dict(handed,elasticsearch_state='absent',elasticsearch_requests=[]),product_api)
                with patch.object(DR,'retire_business',side_effect=retire) as retirement:
                    if version=='9.6.0':
                        with self.assertRaises(ValueError):DR.retire_upgrade_fixture(lab,'compact')
                        retirement.assert_not_called();self.assertTrue(ledger.exists())
                    else:
                        DR.retire_upgrade_fixture(lab,'compact')
                        retirement.assert_called_once_with(lab,'compact');self.assertFalse(ledger.exists())

    def test_upgrade_snapshot_uses_index_format_identity_and_checks_every_source_member(self):
        snapshot={'version':'9.5.2-9.5.5','version_id':9111000,'state':'SUCCESS','uuid':'snapshot-uuid',
                  'indices':['confirmed'], 'shards':{'total':3,'successful':3,'failed':0}}
        nodes={'nodes':{str(i):{'version':'9.5.5','index_version':9111000} for i in range(3)}}
        for defect in ('none','mixed-stack','missing-member','format','partial','failed-shard','empty-shards','wrong-index','missing-uuid'):
            with self.subTest(defect=defect):
                snap, members=copy.deepcopy(snapshot),copy.deepcopy(nodes)
                if defect=='mixed-stack':members['nodes']['0']['version']='9.5.6'
                if defect=='missing-member':members['nodes'].pop('0')
                if defect=='format':snap['version_id']=9112000
                if defect=='partial':snap['state']='PARTIAL'
                if defect=='failed-shard':snap['shards'].update(successful=2,failed=1)
                if defect=='empty-shards':snap['shards'].update(total=0,successful=0)
                if defect=='wrong-index':snap['indices']=['other']
                if defect=='missing-uuid':snap['uuid']=''
                request=Mock(side_effect=[{'snapshots':[snap]},members])
                if defect=='none':self.assertEqual(snap,DR.upgrade_snapshot(request,'confirmed'))
                else:
                    with self.assertRaisesRegex(ValueError,'pre-upgrade snapshot'):DR.upgrade_snapshot(request,'confirmed')

    def test_pinned_previous_manifests_are_real_official_341_and_live_consumers_block_fixture_replacement(self):
        from tests.unit.test_elasticsearch_delivery import load as load_product
        previous=load_product('eck');previous.VERSION='3.4.1';previous.ROOT=ROOT/'tests/fixtures/elasticsearch'
        previous.PINS={'crds':'488f2eda4b0d21637d0a2764f0fced6eafbb7279d1a1fb9c35a9ebaac28e393a',
                       'operator':'acb8a565a3ad932e10091da32f9d5652de390ade105e8b6028314d3140576b8a'}
        manifests=previous.manifests()
        manager=next(row for row in manifests['operator'] if row['kind']=='StatefulSet')['spec']['template']['spec']['containers'][0]
        self.assertTrue(manager['image'].endswith(':3.4.1'))
        self.assertGreater(len(manifests['crds']),1)
        k=Mock()
        def get(kind,*args):
            if kind=='crd':return {'items':[{'spec':{'group':'elasticsearch.k8s.elastic.co','names':{'plural':'elasticsearches'}}}]}
            return {'items':[{'kind':'Elasticsearch','metadata':{'name':'protected','namespace':'business','uid':'consumer'}}]}
        k.get.side_effect=get
        lab=SimpleNamespace(DIRECTORY=Path('/unmutated'),product=Mock())
        with (patch.object(DR.importlib.util,'spec_from_file_location',return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module:None))),
              patch.object(DR.importlib.util,'module_from_spec',return_value=previous),patch.object(previous,'API',return_value=k)):
            with self.assertRaisesRegex(ValueError,'zero Elastic consumers'):DR.previous_eck(lab)
        k.run.assert_not_called();k.apply.assert_not_called();lab.product.assert_not_called()


class FrozenImageCacheContracts(unittest.TestCase):
    def test_default_download_freezes_the_actual_ansible_and_platform_consumers_before_kubecli(self):
        from common.constants import KubeConstant
        from service.cluster.downloader import DownloadManager
        downloader=object.__new__(DownloadManager)
        downloader.kube_constant=KubeConstant();downloader.registry=Mock()
        downloader.docker=SimpleNamespace(is_docker_installed=True)
        downloader.get_ansible_execution_env();downloader.get_default_images()
        consumed={downloader.registry._ensure_image_local.call_args.args[0],
                  *downloader.registry.upload_to_registry.call_args.args[0]}
        self.assertLessEqual(consumed,ARTIFACTS.inventory())
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'BASE',Path(temporary)):
            path=Path(temporary)/'tests/evidence';path.mkdir(parents=True)
            (path/'elasticsearch-images-20261103.json').write_text('{}')
            rows={key:{'key':key,'talkedu_ref':'hub.talkedu.cn/kubeauto/'+key.removeprefix('brinnatt/'),
                       'digest':'sha256:'+'a'*64} for key in consumed}
            client=Mock();client.info.return_value={'DockerRootDir':temporary};staged=set()
            def image(ref):
                key=next(key for key,row in rows.items() if ref==row['talkedu_ref'].rsplit(':',1)[0]+'@'+row['digest'])
                return SimpleNamespace(attrs={'RepoDigests':[ref]},tag=lambda repository,tag:staged.add(repository+':'+tag) or True)
            client.images.get.side_effect=image
            module=SimpleNamespace(records=lambda _:rows)
            def execute(argv,**kwargs):
                self.assertEqual(['kubecli','download','-D'],argv)
                self.assertEqual(consumed,staged)
            with (patch.object(LAB.importlib.util,'spec_from_file_location',return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m:None))),
                  patch.object(LAB.importlib.util,'module_from_spec',return_value=module),patch('docker.from_env',return_value=client),
                  patch.object(LAB,'execute',side_effect=execute)):
                LAB.product('download','-D')
            client.close.assert_called_once()

    def test_cached_legacy_tag_cannot_replace_frozen_digest_and_refusal_blocks_the_product_download(self):
        import docker
        row={'key':'brinnatt/pause:3.10','talkedu_ref':'hub.talkedu.cn/kubeauto/pause:3.10','digest':'sha256:'+'a'*64}
        ref='hub.talkedu.cn/kubeauto/pause@'+row['digest']
        with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'BASE',Path(temporary)):
            path=Path(temporary)/'tests/evidence';path.mkdir(parents=True);(path/'elasticsearch-images-20261103.json').write_text('{}')
            client=Mock();client.info.return_value={'DockerRootDir':temporary}
            client.images.get.side_effect=docker.errors.ImageNotFound('not cached by immutable identity')
            image=Mock();image.attrs={'RepoDigests':[ref]};client.images.pull.return_value=image
            module=SimpleNamespace(records=Mock(return_value={row['key']:row}))
            with (patch.object(LAB.importlib.util,'spec_from_file_location',return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m:None))),
                  patch.object(LAB.importlib.util,'module_from_spec',return_value=module),patch('docker.from_env',return_value=client),
                  patch('shutil.disk_usage',return_value=SimpleNamespace(free=16*1024**3))):
                LAB.stage_images('platform-default')
                client.images.pull.assert_called_once_with(ref);image.tag.assert_called_once_with('brinnatt/pause','3.10')
                image.attrs={'RepoDigests':['hub.talkedu.cn/kubeauto/pause@sha256:'+'b'*64]};image.tag.reset_mock()
                with self.assertRaisesRegex(ValueError,'frozen publication digest'):LAB.stage_images('platform-default')
                image.tag.assert_not_called()
        with patch.object(LAB,'stage_images',side_effect=ValueError('unqualified publication')),patch.object(LAB,'execute') as execute:
            with self.assertRaisesRegex(ValueError,'unqualified publication'):LAB.product('download','-D')
            execute.assert_not_called()


class S3OwnershipContracts(unittest.TestCase):
    def setUp(self):
        import docker
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.owner = 'owned-lease'
        (self.root/'ownership.json').write_text(json.dumps({'id': self.owner}))
        self.path = self.root/'s3'; self.path.mkdir()
        self.record = {'owner': self.owner, 'container_id': 'container-uid', 'directory_identity': [self.path.stat().st_dev, self.path.stat().st_ino]}
        (self.root/'s3.json').write_text(json.dumps(self.record))
        self.server = Mock(id='container-uid', labels={S3.LABEL: self.owner})
        self.client = Mock()
        def get(name):
            if name == S3.NAME: return self.server
            raise docker.errors.NotFound('not present')
        self.client.containers.get.side_effect = get
        p = patch.object(S3, 'STATE', self.root); p.start(); self.addCleanup(p.stop)

    def test_own_container_and_directory_are_removed_only_after_all_checks(self):
        S3.cleanup(self.client)
        self.server.remove.assert_called_once_with(force=True)
        self.assertFalse(self.path.exists()); self.assertFalse((self.root/'s3.json').exists())
        self.assertTrue((self.root/'ownership.json').exists())

    def test_foreign_client_blocks_deletion_of_even_the_owned_server(self):
        client = Mock(labels={S3.LABEL: 'foreign'})
        self.client.containers.get.side_effect = lambda name: self.server if name == S3.NAME else client
        with self.assertRaisesRegex(ValueError, 'foreign S3 client'): S3.cleanup(self.client)
        self.server.remove.assert_not_called(); client.remove.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_replaced_directory_or_container_never_gets_removed(self):
        self.path.rename(self.root/'saved'); self.path.mkdir(); (self.path/'customer').write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'directory was replaced'): S3.cleanup(self.client)
        self.server.remove.assert_not_called()
        self.assertEqual('preserve', (self.path/'customer').read_text())

    def test_foreign_container_uid_refuses_before_removal(self):
        self.server.id = 'foreign-container'
        with self.assertRaisesRegex(ValueError, 'container ownership'): S3.cleanup(self.client)
        self.server.remove.assert_not_called(); self.assertTrue(self.path.exists())


class S3RotationContracts(unittest.TestCase):
    def test_rotation_journals_new_account_before_creation_and_proves_old_denial(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(S3, 'STATE', Path(temporary)):
            root = Path(temporary)
            (root/'ownership.json').write_text(json.dumps({'id': 'lease'}))
            record = {'owner': 'lease', 'access_key': 'old-data-key', 'secret_key': 'old-data-secret'}
            (root/'s3.json').write_text(json.dumps(record))
            calls = []
            def mc(client, current, script, **kwargs):
                self.assertIn('rotation', json.loads((root/'s3.json').read_text()))
                calls.append((script, kwargs))
            with patch.object(S3, 'matching_container', return_value=object()), patch.object(S3, 'verify'), patch.object(S3, 'mc', side_effect=mc):
                S3.rotate(None, 'rotation-prepare')
                S3.rotate(None, 'rotation-disable-old')
                S3.rotate(None, 'rotation-restore')
            self.assertTrue(any(kwargs.get('denied') is True and kwargs['credentials']['access_key'] == 'old-data-key' for script, kwargs in calls))
            self.assertEqual(record, json.loads((root/'s3.json').read_text()))
            self.assertFalse(list(root.glob('*.new')))

    def test_mc_denial_requires_specific_authentication_error_and_always_retires_own_container(self):
        record = {'owner': 'lease', 'admin_access_key': 'admin-key', 'admin_secret_key': 'private-secret'}
        client = Mock(); container = client.containers.create.return_value
        for rc, raw, accepted in ((1, b'Access Denied', True), (0, b'', False), (1, b'certificate signed by unknown authority', False), (1, b'connection timed out', False)):
            container.wait.return_value = {'StatusCode': rc}; container.logs.return_value = raw
            with patch.object(S3, 'image', return_value='pinned-mc'):
                if accepted: S3.mc(client, record, 'mc stat gate/bucket', denied=True)
                else:
                    with self.assertRaisesRegex(ValueError, 'explicit authentication'): S3.mc(client, record, 'mc stat gate/bucket', denied=True)
            container.remove.assert_called_with(force=True)
        self.assertEqual('/bin/sh', client.containers.create.call_args.kwargs['entrypoint'])
        self.assertNotIn('private-secret', str(client.containers.create.call_args.args))

    def test_recovery_rejects_replaced_secret_before_backend_or_product_mutation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            path = Path(temporary)/'fault-s3-rotation.json'
            LAB.atomic(path, {'token': 'a'*16, 'secrets': {'standard': {'uid': 'original', 'data': {}}}})
            k = Mock(); k.get.return_value = {'metadata': {'uid': 'replacement'}}
            with patch.object(LAB, 'api', return_value=k), patch.object(LAB, 'selected_config', return_value={'elasticsearch_namespace': 'es-standard', 'elasticsearch_s3_secret': 'es-gate-s3'}), patch.object(LAB, 's3_fixture') as fixture, patch.object(LAB, 'product') as product:
                with self.assertRaisesRegex(ValueError, 'replaced credential'): LAB.restore_s3_rotation()
            fixture.assert_not_called(); product.assert_not_called(); k.apply.assert_not_called()
            self.assertTrue(path.exists())

    def test_recovery_restores_backend_then_original_secret_then_reload_and_removes_only_own_snapshot(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            path = Path(temporary)/'fault-s3-rotation.json'
            data = {'s3.client.default.access_key': 'encoded-original'}
            LAB.atomic(path, {'token': 'a'*16, 'secrets': {'standard': {'uid': 'original', 'data': data}}, 'snapshots': {'standard': 'rotation-'+'a'*16}})
            events = []; snapshot = {'metadata': {'kubeauto_change_id': 'rotation-'+'a'*16}}
            def request(method, route):
                events.append(method)
                if method == 'DELETE': snapshot.clear(); return {'acknowledged': True}
                return {'snapshots': [copy.deepcopy(snapshot)] if snapshot else []}
            k = Mock(); k.get.return_value = {'metadata': {'uid': 'original'}}
            k.apply.side_effect = lambda objects: events.append(('secret', objects[0]['data']))
            with patch.object(LAB, 'api', return_value=k), patch.object(LAB, 'selected_config', return_value={'elasticsearch_namespace': 'es-standard', 'elasticsearch_s3_secret': 'es-gate-s3'}), patch.object(LAB, 's3_fixture', side_effect=lambda a: events.append(a)), patch.object(LAB, 'product', side_effect=lambda *a, **kw: events.append('product-reload')), patch.object(LAB, 'wait_repository', side_effect=lambda t: events.append('verify')), patch.object(LAB, 'data_ledger'), patch.object(LAB, 'load_es', return_value=SimpleNamespace(connection=lambda *a: nullcontext(request))):
                LAB.restore_s3_rotation()
            self.assertEqual(['rotation-restore', ('secret', data), 'product-reload', 'verify', 'GET', 'DELETE', 'GET'], events)
            self.assertFalse(path.exists())


class ClientCleanupContracts(unittest.TestCase):
    def test_uncertain_create_is_recorded_and_cleaned_without_masking_first_error(self):
        events = []
        def call(client, method, path, body=None, **kwargs):
            events.append((method, path))
            if method == 'GET': return {'error': {'type': 'index_not_found_exception'}}
            if method == 'PUT': raise ValueError('first unknown create outcome')
            if method == 'DELETE': raise ValueError('secondary cleanup failure')
            raise AssertionError((method, path))
        with patch.object(BUSINESS, 'call', side_effect=call), redirect_stderr(StringIO()):
            with self.assertRaisesRegex(ValueError, 'first unknown create'): BUSINESS.business(None, 'es-gate-'+'a'*16)
        self.assertEqual(['GET', 'PUT', 'DELETE'], [m for m,p in events])

    def test_foreign_exact_index_is_never_registered_for_cleanup(self):
        with patch.object(BUSINESS, 'call', side_effect=ValueError('expected absence refused')) as call:
            with self.assertRaisesRegex(ValueError, 'expected absence'): BUSINESS.business(None, 'es-gate-'+'a'*16)
        self.assertEqual(1, call.call_count)
        self.assertEqual('GET', call.call_args.args[1])


class PerformanceContracts(unittest.TestCase):
    def test_rally_main_uses_the_pinned_report_option_and_erases_private_client_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'work').mkdir()
            def local_path(name): return root/Path(name).name
            for name, value in (('username', 'private-user'), ('password', 'private-password')):
                (root/name).write_text(value)
            def run(argv, **kwargs):
                if argv == ['esrally', '--version']:
                    return SimpleNamespace(returncode=0, stdout='esrally 2.13.0')
                if argv[0] == 'sha256sum': return SimpleNamespace(returncode=0)
                self.assertEqual(['esrally', 'race'], argv[:2])
                self.assertIn('--show-in-report=all-percentiles', argv)
                self.assertFalse(any(arg.startswith('--report-values') for arg in argv))
                self.assertNotIn('private-password', ' '.join(argv))
                options = json.loads((root/'work/client-options.json').read_text())
                self.assertEqual('private-password', options['default']['basic_auth_password'])
                return SimpleNamespace(returncode=2)
            with (patch.object(RALLY, 'Path', side_effect=local_path), patch.object(RALLY.shutil, 'copytree'),
                  patch.object(RALLY.subprocess, 'run', side_effect=run),
                  patch.dict(RALLY.os.environ, {'ELASTICSEARCH_URL': 'https://search.example:9200'})):
                with self.assertRaisesRegex(ValueError, 'Rally race failed'): RALLY.main()
            self.assertFalse((root/'work/client-options.json').exists())

    def test_rally_report_requires_real_positive_task_metrics_and_zero_errors(self):
        import csv
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'report.csv'
            def write(error=0, omit=None):
                with path.open('w') as stream:
                    writer = csv.writer(stream); writer.writerow(['Metric', 'Task', 'Value', 'Unit'])
                    for task in ('bulk', 'search'):
                        writer.writerow(['error rate', task, error, '%'])
                        writer.writerow(['Median Throughput', task, 10, 'ops/s'])
                    for p in (50, 99):
                        if omit != p: writer.writerow([str(p)+'th percentile latency', 'search', p/10, 'ms'])
            write(); result = RALLY.report(path)
            self.assertEqual(5, result['search']['50th percentile latency']['value'])
            self.assertNotIn('95th percentile latency', result['search'])
            write(error=.1)
            with self.assertRaisesRegex(ValueError, 'request errors'): RALLY.report(path)
            write(omit=99)
            with self.assertRaisesRegex(ValueError, 'percentile'): RALLY.report(path)

    def test_soak_and_rally_jobs_have_real_deadlines_and_writable_owned_state(self):
        c = {'elasticsearch_namespace': 'es-standard', 'elasticsearch_name': 'search', 'elasticsearch_admin_secret': 'rally-admin', 'elasticsearch_http_tls_secret': ''}
        with patch.object(LAB, 'BASE', ROOT), patch.object(LAB, 'load_es', return_value=SimpleNamespace(LABELS={})):
            cm, job = LAB.client_objects(c, 'soak', 'a'*16)
            self.assertGreaterEqual(job['spec']['activeDeadlineSeconds'], 1800)
            cm, job = LAB.client_objects(c, 'rally', 'b'*16)
        self.assertEqual((ROOT/'tests/helpers/elasticsearch-rally.py').read_text(), cm['data']['run.py'])
        pod = job['spec']['template']['spec']
        self.assertEqual(1000, pod['securityContext']['fsGroup'])
        volumes = {v['name']: v for v in pod['volumes']}
        self.assertIn('emptyDir', volumes['work']); self.assertIn('emptyDir', volumes['rally-state'])
        self.assertTrue(pod['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(['python3', '/scripts/run.py'], pod['containers'][0]['command'])

    def test_percentile_is_based_on_samples_and_never_made_up_from_p90_p99(self):
        self.assertEqual(5, BUSINESS.percentile([0, 10], 50))
        self.assertEqual(9.5, BUSINESS.percentile([0, 10], 95))
        with self.assertRaises(ValueError): BUSINESS.percentile([], 95)

    def test_rally_corpus_content_hash_matches_the_published_frozen_fixture(self):
        import bz2, hashlib
        path = ROOT.parent/'kubeauto-ext-images-dockerfile/middleware/elasticsearch/rally/track/documents.json.bz2'
        rows = sorted((json.loads(line) for line in bz2.open(path, 'rt')), key=lambda row: row['sequence'])
        actual = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(10000, len(rows))
        self.assertEqual('572c88638ae1d75a097fa43f0e8c2f45120960482d642016cccf870f9edc93cf', actual)
        self.assertIn(actual, (ROOT/'tests/helpers/elasticsearch-lab.py').read_text())


class MatrixEvidenceContracts(unittest.TestCase):
    def setUp(self):
        import yaml
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root/'tests').mkdir()
        self.path = self.root/'tests/matrix.yaml'
        self.data = yaml.safe_load((ROOT/'tests/elasticsearch-test-matrix.yaml').read_text())
        for c in self.data['cases']:
            c.update(status='pending', evidence=[])
            c.pop('retained_evidence',None)
        self.data['acceptance']['accepted'] = 0; self.data['meta']['result'] = 'PENDING'

    def check(self):
        import yaml
        self.path.write_text(yaml.safe_dump(self.data))
        return MATRIX.validate(self.path)

    def binding(self, filename, content):
        import hashlib
        path = self.root/filename; path.parent.mkdir(exist_ok=True, parents=True); path.write_text(content)
        return {'path': filename, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def evidence(self):
        case = self.data['cases'][0]
        raw = self.binding('logs/raw.log', case['success_marker']+'\nELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS\n')
        dependency = self.binding('product.py', '# pinned production input\n')
        record = {'case_ids': ['ES-01'], 'durable_rc': 0, 'failure_markers': 0, 'scoped_cleanup': True,
                  'product_entry': case['product_entry'], 'scoped_cleanup_marker': 'ELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS',
                  'dependencies': {dependency['path']: dependency['sha256']}, 'raw_logs': [raw],
                  'durable': {key: self.binding('logs/run.'+key, '0\n') for key in ('exit', 'finalized')}}
        path = self.root/'tests/evidence.json'; path.write_text(json.dumps(record))
        case.update(status='pass', evidence=['tests/evidence.json']); self.data['acceptance']['accepted'] = 1
        return record, path

    def test_all_59_items_and_acyclic_stage_order_are_mandatory(self):
        self.assertEqual([], self.check())
        self.data['cases'].pop(); self.data['stages'][-1]['cases'].pop(); self.data['acceptance']['applicable'] -= 1
        self.assertTrue(any('59' in e for e in self.check()))
        self.data['stages'][0]['depends_on'] = ['delivery']
        self.assertTrue(any('cycle' in e for e in self.check()))

    def test_disproof_uses_actual_focused_owner_instead_of_unimplemented_group(self):
        self.assertEqual([], self.check())
        runner=(ROOT/'tests/helpers/elasticsearch-runner.sh').read_text()
        for case in self.data['cases']:
            mode=case['disproof'].split()[-1]
            with self.subTest(case=case['id']):
                self.assertIn(mode,runner)
        for case_id, invalid in (('ES-07','platform'),('ES-55','coexistence'),
                                 ('ES-56','cleanup'),('ES-59','delivery')):
            case=next(row for row in self.data['cases'] if row['id']==case_id)
            original=case['disproof']
            case['disproof']='bash tests/run_enterprise_regression.sh --elasticsearch-'+invalid
            with self.subTest(case=case_id):
                self.assertIn(case_id+': disproof must use the fixed module entry',self.check())
            case['disproof']=original

    def test_real_raw_case_cleanup_dependency_and_fenced_exit_bindings_are_required(self):
        record, path = self.evidence()
        self.assertEqual([], self.check())
        for mutation in ('marker', 'cleanup', 'fence', 'dependency', 'escape'):
            copy_record = copy.deepcopy(record)
            if mutation == 'marker': copy_record['raw_logs'] = [self.binding('logs/incorrect.log', 'other case\n')]
            elif mutation == 'cleanup': copy_record['scoped_cleanup_marker'] = 'MISSING_CLEANUP'
            elif mutation == 'fence': copy_record['durable']['finalized'] = self.binding('logs/failed.finalized', '1\n')
            elif mutation == 'dependency': copy_record['dependencies']['product.py'] = '0'*64
            else: copy_record['raw_logs'][0]['path'] = '../outside.log'
            path.write_text(json.dumps(copy_record))
            with self.subTest(mutation=mutation): self.assertTrue(self.check())

    def test_empty_or_summary_only_evidence_never_signs_a_case(self):
        record, path = self.evidence()
        for field in ('dependencies', 'raw_logs', 'durable'):
            modified = dict(record); modified[field] = {} if field != 'raw_logs' else []
            path.write_text(json.dumps(modified))
            with self.subTest(field=field): self.assertTrue(self.check())


class IsolationMatrixContracts(unittest.TestCase):
    def fixture(self):
        fixture=MatrixEvidenceContracts()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        return fixture

    def test_all_fifteen_scenarios_require_explicit_guards_data_and_cleanup_contracts(self):
        fixture=self.fixture()
        self.assertEqual([],fixture.check())
        count=0
        for case in fixture.data['cases']:
            if case['id'] not in MATRIX.ISOLATION_SUBCASES:continue
            original=copy.deepcopy(case['subcases']);count+=len(original)
            case['subcases']=original[:-1]
            self.assertTrue(any('mandatory isolation subcases' in error for error in fixture.check()))
            for field in ('preconditions','product_entry','operation','code_guard','expected','cleanup_scope'):
                case['subcases']=copy.deepcopy(original);case['subcases'][0][field]=''
                self.assertTrue(any('subcase missing '+field in error for error in fixture.check()))
            case['subcases']=copy.deepcopy(original)
            case['subcases'][0]['success_marker']=case['success_marker']
            self.assertTrue(any('subcase marker' in error for error in fixture.check()))
            case['subcases']=original
        self.assertEqual(15,count)
        self.assertEqual([],fixture.check())

    def test_every_missing_child_blocks_publication_reuse_and_full_composition(self):
        import shutil
        import yaml
        fixture=self.fixture();root=fixture.root
        (root/'tests/helpers').mkdir();(root/'tests/evidence').mkdir()
        shutil.copy(ROOT/'tests/helpers/validate_elasticsearch_matrix.py',root/'tests/helpers')
        fixture.path=root/'tests/elasticsearch-test-matrix.yaml';fixture.check()
        product=root/'product.py';product.write_text('pinned input')
        inputs={'product.py':EVIDENCE.digest(product)}
        for case in fixture.data['cases']:
            if not case.get('subcases'):continue
            module=MATRIX.CASE_MODULES[case['id']]
            terminal='ELASTICSEARCH_MODULE_PASS module='+module
            complete=[case['success_marker']]+[row['success_marker'] for row in case['subcases']]+[terminal]
            record={'case_ids':[case['id']],'durable_rc':0,'failure_markers':0,
                'product_entry':case['product_entry'],'scoped_cleanup':True,'scoped_cleanup_marker':terminal,
                'dependencies':inputs,'raw_logs':[],
                'durable':{name:fixture.binding('logs/'+name,'0\n') for name in ('exit','finalized')}}
            for index,missing in enumerate(complete[1:-1]):
                with self.subTest(case=case['id'],missing=missing):
                    directory=root/'logs'/(case['id']+'-missing-'+str(index));directory.mkdir()
                    (directory/'launch-inputs.json').write_text(json.dumps(inputs))
                    raw='\n'.join(line for line in complete if line!=missing)+'\n'
                    record['raw_logs']=[fixture.binding(str((directory/'raw.log').relative_to(root)),raw)]
                    for name in ('exit','finalized'):(directory/name).write_text('0\n')
                    with patch.object(EVIDENCE,'dependencies',return_value=inputs),self.assertRaisesRegex(ValueError,'subcase'):
                        EVIDENCE.publish(root,directory,terminal,module)
                    self.assertFalse((root/'tests/evidence'/('elasticsearch-'+directory.name+'.json')).exists())
                    path=root/'tests/evidence/old.json';path.write_text(json.dumps(record))
                    with self.assertRaisesRegex(ValueError,'subcase'):MATRIX.verify_evidence(root,'tests/evidence/old.json',case)
                    with self.assertRaisesRegex(ValueError,'subcase'):FULL.module_cases(raw,module,EVIDENCE.SCOPES,fixture.data)
            directory=root/'logs'/(case['id']+'-complete');directory.mkdir()
            (directory/'launch-inputs.json').write_text(json.dumps(inputs))
            (directory/'raw.log').write_text('\n'.join(complete)+'\n')
            for name in ('exit','finalized'):(directory/name).write_text('0\n')
            with patch.object(EVIDENCE,'dependencies',return_value=inputs):
                self.assertEqual([case['id']],EVIDENCE.publish(root,directory,terminal,module))
            self.assertEqual({case['id']},FULL.module_cases('\n'.join(complete),module,EVIDENCE.SCOPES,fixture.data))
            self.assertEqual([],EVIDENCE.refresh(root))
            ledger=yaml.safe_load(fixture.path.read_text())
            accepted=next(row for row in ledger['cases'] if row['id']==case['id'])
            receipt=root/accepted['evidence'][0];current=json.loads(receipt.read_text())
            current['raw_logs']=[fixture.binding(str((directory/'raw.log').relative_to(root)),case['success_marker']+'\n'+terminal+'\n')]
            receipt.write_text(json.dumps(current))
            self.assertEqual([case['id']],EVIDENCE.refresh(root))
            refreshed=yaml.safe_load(fixture.path.read_text())
            self.assertIn('mandatory isolation subcases',next(row for row in refreshed['cases'] if row['id']==case['id'])['pending_reason'])


class EvidenceRetentionContracts(unittest.TestCase):
    def test_tls_wait_reuses_only_authenticated_unchanged_success_paths(self):
        import ast
        name = 'roles/cluster-addon/files/elasticsearch.py'
        current = (ROOT/name).read_text()
        tree = ast.parse(current)
        health = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'health')
        ready = next(node for node in ast.walk(health) if isinstance(node, ast.If) and
                     isinstance(node.test, ast.Name) and node.test.id == 'ready')
        self.assertIsInstance(ready.body[0], ast.Try)
        ready.body[:1] = ready.body[0].body
        legacy = ast.unparse(tree)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); product = root/name; product.parent.mkdir(parents=True)
            product.write_text(legacy)
            inputs = {name:EVIDENCE.digest(product)}
            directory = root/'logs/original'
            with patch.object(EVIDENCE, 'launch_dependencies', return_value=inputs):
                EVIDENCE.freeze(root, directory, 'standard')
            receipt = {'provenance':[EVIDENCE.binding(root, directory/'launch-sources.tar.gz')]}
            product.write_text(current)
            for cid in ('ES-10', 'ES-24', 'ES-28', 'ES-39'):
                self.assertTrue(MATRIX.dependency_matches(root, receipt, {'id':cid}, name, inputs[name]))
            for cid in ('ES-26', 'ES-59'):
                self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':cid}, name, inputs[name]))
            self.assertFalse(MATRIX.dependency_matches(root, {}, {'id':'ES-10'}, name, inputs[name]))
            self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':'ES-10'}, name, '0'*64))
            for changed in (
                    current.replace('except ssl.SSLCertVerificationError:', 'except ssl.SSLError:'),
                    current.replace('reason=certificate-convergence', 'reason=ignored'),
                    current.replace('deadline = time.monotonic() + c["elasticsearch_wait_seconds"]',
                                    'deadline = time.monotonic() + 999999'),
                    current.replace('SSLContext(ssl.PROTOCOL_TLS_SERVER)', 'SSLContext(ssl.PROTOCOL_TLS_CLIENT)'),
                    current.replace('cluster UUID differs from the retained data identity', 'changed identity check')):
                with self.subTest(change=changed):
                    self.assertNotEqual(current, changed)
                    product.write_text(changed)
                    self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':'ES-10'}, name, inputs[name]))

    def test_reverted_dependencies_restore_only_accepted_immutable_current_receipts(self):
        import yaml
        for defect in ('none','dependency','log','exit','finalized','never-accepted','failed'):
            with self.subTest(defect=defect):
                fixture=self.fixture();record,path=fixture.evidence();fixture.check()
                original=path.read_bytes();source=fixture.root/'product.py';pinned=source.read_bytes()
                source.write_text('# rejected change\n')
                self.assertEqual(['ES-01'],EVIDENCE.refresh(fixture.root))
                source.write_bytes(pinned)
                data=yaml.safe_load(fixture.path.read_text());case=data['cases'][0]
                if defect=='dependency':source.write_text('# still changed\n')
                elif defect=='log':(fixture.root/record['raw_logs'][0]['path']).write_text('missing markers\n')
                elif defect in ('exit','finalized'):(fixture.root/record['durable'][defect]['path']).write_text('1\n')
                elif defect=='never-accepted':case.pop('pending_reason')
                elif defect=='failed':case['status']='fail'
                fixture.path.write_text(yaml.safe_dump(data))
                self.assertEqual([],EVIDENCE.refresh(fixture.root))
                current=yaml.safe_load(fixture.path.read_text())
                self.assertEqual(1 if defect=='none' else 0,current['acceptance']['accepted'])
                self.assertEqual('pass' if defect=='none' else ('fail' if defect=='failed' else 'pending'),current['cases'][0]['status'])
                self.assertEqual(original,path.read_bytes())
                if defect=='none':self.assertEqual([],MATRIX.validate(fixture.path))

    def test_aggregate_receipt_reuse_authenticates_source_then_isolates_owning_module(self):
        name = 'tests/helpers/elasticsearch-lab.py'
        for aggregate, unchanged, affected in (
                ('security', ('ES-24', 'ES-25', 'ES-26', 'ES-27', 'ES-29'), ('ES-28',)),
                ('backup', ('ES-40', 'ES-41', 'ES-42'), ('ES-39',))):
            with self.subTest(aggregate=aggregate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); source = root/name; source.parent.mkdir(parents=True)
                original = (ROOT/name).read_text(); source.write_text(original)
                entry = name+'#'+aggregate
                inputs = {entry:MATRIX.dependency_digest(root, entry)}
                directory = root/'logs/original'
                with patch.object(EVIDENCE, 'launch_dependencies', return_value=inputs):
                    EVIDENCE.freeze(root, directory, aggregate)
                receipt = {'provenance':[EVIDENCE.binding(root, directory/'launch-sources.tar.gz')]}
                source.write_text(original.replace('def configure_s3():', 'def configure_s3():\n    raise ValueError("changed S3 setup")'))
                for cid in unchanged:
                    self.assertTrue(MATRIX.dependency_matches(root, receipt, {'id':cid}, entry, inputs[entry]))
                for cid in affected:
                    self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':cid}, entry, inputs[entry]))
                self.assertFalse(MATRIX.dependency_matches(root, {}, {'id':unchanged[0]}, entry, inputs[entry]))
                self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':unchanged[0]}, entry, '0'*64))
                source.write_text(source.read_text().replace('def data_ledger(topology, *, create=False):',
                    'def data_ledger(topology, *, create=False):\n    raise ValueError("changed business verifier")'))
                self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':unchanged[0]}, entry, inputs[entry]))

    def test_minio_opt_in_preserves_only_unchanged_default_evidence(self):
        option = 'elasticsearch_repository_analysis_copy_during_write_contention'
        name = 'roles/cluster-addon/files/elasticsearch.py'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = root/name
            product.parent.mkdir(parents=True)
            current = (ROOT/name).read_text()
            validation = '    flag(c.get("'+option+'", True))\n'
            branch = ('        if not flag(c.get("'+option+'", True)):\n'
                      '            sets[-1]["config"]["repositories.blobstore.testkit.analyze.copy_during_write_contention"] = False\n')
            legacy = current.replace(validation, '').replace(branch, '')
            self.assertNotEqual(legacy, current)
            product.write_text(legacy)
            inputs = {name:EVIDENCE.digest(product)}
            directory = root/'logs/original'
            with patch.object(EVIDENCE, 'launch_dependencies', return_value=inputs):
                EVIDENCE.freeze(root, directory, 'standard')
            receipt = {'provenance':[EVIDENCE.binding(root, directory/'launch-sources.tar.gz')]}
            product.write_text(current)
            self.assertTrue(MATRIX.dependency_matches(root, receipt, {'id':'ES-10'}, name, inputs[name]))
            self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':'ES-59'}, name, inputs[name]))
            self.assertFalse(MATRIX.dependency_matches(root, {}, {'id':'ES-10'}, name, inputs[name]))
            for changed in (current.replace('c.get("'+option+'", True)', 'c.get("'+option+'", False)'),
                            current.replace('max_blob_size=1mb', 'max_blob_size=2mb'),
                            current.replace('node.store.allow_mmap": True', 'node.store.allow_mmap": False')):
                product.write_text(changed)
                self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':'ES-10'}, name, inputs[name]))
            product.write_text(current)
            (root/'conf').mkdir()
            (root/'conf/config.yml').write_text(option+': false\n')
            self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':'ES-10'}, name, inputs[name]))

    def fixture(self):
        import shutil
        fixture=MatrixEvidenceContracts();fixture.setUp();self.addCleanup(fixture.doCleanups)
        (fixture.root/'tests/helpers').mkdir()
        shutil.copy(ROOT/'tests/helpers/validate_elasticsearch_matrix.py',fixture.root/'tests/helpers')
        fixture.path=fixture.root/'tests/elasticsearch-test-matrix.yaml'
        return fixture

    def test_unit_changes_do_not_invalidate_live_results_but_product_and_contract_changes_do(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);unit=root/'tests/unit/test_elasticsearch_gate.py';unit.parent.mkdir(parents=True)
            unit.write_text('# original local contract\n');checksum=EVIDENCE.digest(unit)
            unit.write_text('# changed local contract\n')
            self.assertTrue(MATRIX.dependency_matches(root,{}, {'id':'ES-10'},str(unit.relative_to(root)),checksum))
            for cid in ('ES-01','ES-59'):
                self.assertFalse(MATRIX.dependency_matches(root,{}, {'id':cid},str(unit.relative_to(root)),checksum))
            product=root/'production.py';product.write_text('# original production\n');checksum=EVIDENCE.digest(product)
            product.write_text('# changed production\n')
            self.assertFalse(MATRIX.dependency_matches(root,{}, {'id':'ES-10'},'production.py',checksum))

    def test_pending_keeps_original_receipt_without_raising_current_pass_count(self):
        fixture=self.fixture();record,path=fixture.evidence();fixture.check()
        original=path.read_bytes()
        (fixture.root/'product.py').write_text('# changed product\n')
        self.assertEqual(['ES-01'],EVIDENCE.refresh(fixture.root))
        import yaml
        data=yaml.safe_load(fixture.path.read_text());case=data['cases'][0]
        self.assertEqual(0,data['acceptance']['accepted'])
        self.assertEqual('pending',case['status']);self.assertEqual([],case['evidence'])
        self.assertEqual(['tests/evidence.json'],case['retained_evidence'])
        self.assertEqual(original,path.read_bytes())
        MATRIX.verify_evidence(fixture.root,'tests/evidence.json',case,historical=True)
        with self.assertRaises(ValueError):MATRIX.verify_evidence(fixture.root,'tests/evidence.json',case)
        self.assertEqual([],MATRIX.validate(fixture.path))

    def test_closed_failed_parent_recovers_only_independently_successful_children(self):
        import hashlib,io,tarfile
        import yaml
        for defect in ('none','child-exit','missing-marker','source-tamper'):
            with self.subTest(defect=defect):
                fixture=self.fixture();root=fixture.root;fixture.check();(root/'tests/evidence').mkdir()
                parent=root/'logs/elasticsearch-cases'/('only-20261105-114006-'+'a'*32);parent.mkdir(parents=True)
                for name in ('exit','finalized'):(parent/name).write_text('143\n')
                raw=b'# pinned original product\n'
                inputs={'product.py':hashlib.sha256(raw).hexdigest()}
                (parent/'launch-inputs.json').write_text(json.dumps(inputs))
                with tarfile.open(parent/'launch-sources.tar.gz','w:gz') as archive:
                    item=tarfile.TarInfo('product.py');value=b'# changed bytes\n' if defect=='source-tamper' else raw
                    item.size=len(value);archive.addfile(item,io.BytesIO(value))
                child_log='ELASTICSEARCH_CASE_PASS id=ES-15\n'
                if defect!='missing-marker':child_log+='ELASTICSEARCH_MODULE_PASS module=business\n'
                buffer=io.BytesIO()
                with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
                    for name,value in {'business.log':child_log,'business.exit':'1\n' if defect=='child-exit' else '0\n','business.finalized':'0\n'}.items():
                        value=value.encode();item=tarfile.TarInfo(name);item.size=len(value);archive.addfile(item,io.BytesIO(value))
                response=subprocess.CompletedProcess([],0,stdout=buffer.getvalue(),stderr=b'')
                with patch.object(EVIDENCE.subprocess,'run',return_value=response) as collect:
                    if defect=='source-tamper':
                        with self.assertRaisesRegex(ValueError,'original parent source'):EVIDENCE.recover_children(root,parent)
                    else:
                        EVIDENCE.recover_children(root,parent)
                    self.assertIn('root@192.168.122.2',collect.call_args.args[0])
                    self.assertNotIn('setup',collect.call_args.kwargs['input'].decode())
                self.assertEqual('143\n',(parent/'exit').read_text())
                self.assertEqual('143\n',(parent/'finalized').read_text())
                data=yaml.safe_load(fixture.path.read_text());case=data['cases'][14]
                self.assertEqual(0,data['acceptance']['accepted']);self.assertEqual('pending',case['status'])
                self.assertEqual(defect=='none',bool(case.get('retained_evidence')))
                if defect=='none':
                    filename=case['retained_evidence'][0]
                    MATRIX.verify_evidence(root,filename,case,historical=True)
                    with self.assertRaisesRegex(ValueError,'historical completion'):MATRIX.verify_evidence(root,filename,case)
                    self.assertEqual([],MATRIX.validate(fixture.path))

    def test_closed_child_uses_its_own_frozen_inputs_for_current_acceptance(self):
        import hashlib,io,tarfile,yaml
        for defect in ('none','running-parent','changed-input','child-exit','missing-subcase','archive-tamper'):
            with self.subTest(defect=defect):
                fixture=self.fixture();root=fixture.root;fixture.check();(root/'tests/evidence').mkdir()
                run='b'*32;parent=root/'logs/elasticsearch-cases'/('only-20261105-114006-'+run);parent.mkdir(parents=True)
                for name in ('exit','finalized'):(parent/name).write_text('143\n')
                if defect=='running-parent':
                    for name in ('exit','finalized'):(parent/name).unlink()
                source=root/'product.py';source.write_text('# frozen child\n')
                checksum=EVIDENCE.digest(source);inputs={'product.py':checksum}
                (parent/'launch-inputs.json').write_text(json.dumps(inputs))
                def archive_source(value):
                    buffer=io.BytesIO()
                    with tarfile.open(fileobj=buffer,mode='w:gz') as stream:
                        item=tarfile.TarInfo('product.py');item.size=len(value);stream.addfile(item,io.BytesIO(value))
                    return buffer.getvalue()
                (parent/'launch-sources.tar.gz').write_bytes(archive_source(source.read_bytes()))
                case=fixture.data['cases'][49]
                raw='\n'.join([case['success_marker']]+[row['success_marker'] for row in case['subcases']]+['ELASTICSEARCH_MODULE_PASS module=upgrade'])+'\n'
                if defect=='missing-subcase':raw=raw.replace(case['subcases'][0]['success_marker']+'\n','')
                files={'upgrade.log':raw.encode(),'upgrade.exit':b'1\n' if defect=='child-exit' else b'0\n','upgrade.finalized':b'0\n',
                    'checkpoints/upgrade/launch-inputs.json':json.dumps(inputs).encode(),
                    'checkpoints/upgrade/launch-provenance.json':json.dumps(inputs).encode(),
                    'checkpoints/upgrade/launch-sources.tar.gz':archive_source(b'# tampered\n' if defect=='archive-tamper' else source.read_bytes())}
                buffer=io.BytesIO()
                with tarfile.open(fileobj=buffer,mode='w:gz') as stream:
                    for name,value in files.items():
                        item=tarfile.TarInfo(name);item.size=len(value);stream.addfile(item,io.BytesIO(value))
                if defect=='changed-input':source.write_text('# changed real product\n')
                response=subprocess.CompletedProcess([],0,stdout=buffer.getvalue(),stderr=b'')
                with patch.object(EVIDENCE.subprocess,'run',return_value=response):
                    if defect=='archive-tamper':
                        with self.assertRaisesRegex(ValueError,'original child source'):EVIDENCE.recover_children(root,parent)
                    else:
                        EVIDENCE.recover_children(root,parent)
                        # Repeated collection is idempotent and never changes the parent exit.
                        if defect=='running-parent':
                            self.assertFalse((parent/'exit').exists())
                            for name in ('exit','finalized'):(parent/name).write_text('143\n')
                        EVIDENCE.recover_children(root,parent)
                data=yaml.safe_load(fixture.path.read_text());current=data['cases'][49]
                accepted=defect in ('none','running-parent')
                self.assertEqual('pass' if accepted else 'pending',current['status'])
                self.assertEqual(1 if accepted else 0,data['acceptance']['accepted'])
                self.assertEqual('143\n',(parent/'exit').read_text())
                if accepted:
                    record=MATRIX.verify_evidence(root,current['evidence'][0],current)
                    self.assertEqual(inputs,record['dependencies'])

    def test_final_launcher_refuses_any_unaccepted_prerequisite_before_remote_launch(self):
        import os,shutil,yaml
        for cid in ('ES-07','ES-24','ES-48','ES-53','ES-58'):
            with self.subTest(cid=cid):
                fixture=self.fixture();root=fixture.root
                data=fixture.data
                for case in data['cases'][:58]:case.update(status='pass',evidence=['missing-receipt'])
                next(case for case in data['cases'] if case['id']==cid).update(status='pending',evidence=[])
                fixture.path.write_text(yaml.safe_dump(data))
                shutil.copy(ROOT/'tests/helpers/elasticsearch-evidence.py',root/'tests/helpers')
                import shlex
                binary=root/'.venv/bin/python';binary.parent.mkdir(parents=True)
                binary.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');binary.chmod(0o700)
                script='set -eu\nROOT="$1"\nsource "$2"\nelasticsearch_local_gate() { :; }\nelasticsearch_launch() { touch "$ROOT/forbidden-launch"; }\nelasticsearch_dispatch --elasticsearch-only\n'
                result=subprocess.run(['bash','-c',script,'contract',str(root),str(ROOT/'tests/helpers/elasticsearch-runner.sh')],
                    env=dict(os.environ),capture_output=True,text=True,timeout=10)
                self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertIn(cid,result.stderr)
                self.assertFalse((root/'forbidden-launch').exists())

    def test_progress_uses_matrix_ids_and_never_treats_historical_or_child_counts_as_pass(self):
        fixture=self.fixture();data=fixture.data
        for case in data['cases'][:6]:case['status']='pass'
        data['cases'][6]['retained_evidence']=['historical.json']
        data['acceptance']['accepted']=6
        status=MATRIX.progress(data)
        self.assertEqual((6,59,53,0,2,18),tuple(status[key] for key in ('accepted','applicable','remaining','failed','accepted_stages','planned_stages')))
        self.assertEqual(['ES-07','ES-08','ES-09'],status['stages'][2]['remaining'])
        self.assertEqual('pending',status['cases'][49]['subcases'][0]['status'])
        data['cases'][0]['status']='pending'
        with self.assertRaisesRegex(ValueError,'summary disagrees'):MATRIX.progress(data)

    def test_final_preparation_verifies_raw_and_current_inputs_for_all_58_items(self):
        import yaml
        fixture=self.fixture();root=fixture.root;(root/'tests/evidence').mkdir()
        record,_=fixture.evidence()
        log='\n'.join([row['success_marker'] for case in fixture.data['cases'][:58]
                       for row in [case,*case.get('subcases',[])]]+['ELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS'])+'\n'
        record['raw_logs']=[fixture.binding('logs/raw.log',log)]
        for case in fixture.data['cases'][:58]:
            path=root/'tests/evidence'/(case['id']+'.json')
            path.write_text(json.dumps(dict(record,case_ids=[case['id']],product_entry=case['product_entry'])))
            case.update(status='pass',evidence=[str(path.relative_to(root))])
        fixture.path.write_text(yaml.safe_dump(fixture.data))
        with patch.object(EVIDENCE,'dependencies',return_value=record['dependencies']):EVIDENCE.prepare_full(root)
        prepared=root/'tests/evidence/elasticsearch-full-prerequisites.json'
        self.assertEqual(58,len(json.loads(prepared.read_text())['evidence']))
        original=prepared.read_bytes()
        (root/'product.py').write_text('# actual product changed\n')
        with self.assertRaisesRegex(ValueError,'dependency differs'):EVIDENCE.prepare_full(root)
        self.assertEqual(original,prepared.read_bytes())


class DurableModuleContracts(unittest.TestCase):
    def setUp(self):
        import shutil
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ('tests/helpers', 'roles/cluster-addon/files', '.venv/bin', 'kube-bin', 'clusters/k8s-dev', '.kube', 'work'):
            (self.root/name).mkdir(parents=True, exist_ok=True)
        for filename in ('elasticsearch.py',):
            shutil.copy(ROOT/'roles/cluster-addon/files'/filename, self.root/'roles/cluster-addon/files'/filename)
        code = (ROOT/'tests/helpers/elasticsearch-lab.py').read_text().replace("BASE = Path('/usr/local/kubeauto')", 'BASE = Path('+repr(str(self.root))+')').replace("STATE = Path('/var/tmp/kubeauto-elasticsearch')", 'STATE = Path('+repr(str(self.root/'state'))+')')
        (self.root/'tests/helpers/elasticsearch-lab.py').write_text(code)
        import shlex
        interpreter = self.root/'.venv/bin/python'
        interpreter.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        interpreter.chmod(0o700)
        (self.root/'clusters/k8s-dev/kubectl.kubeconfig').write_text('owned fixture context')
        self.context = self.root/'.kube/config'; self.context.write_bytes(b'preserve original context\n')
        self.context.chmod(0o600)
        stub = self.root/'kube-bin/kubectl'
        stub.write_text('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\nvalues=json.loads(Path('+repr(str(self.root/'responses.json'))+').read_text())\nargs=sys.argv[1:]; kind=args[args.index("get")+1]\nprint(json.dumps(values.get(kind, {"items": []})))\n')
        stub.chmod(0o700)
        # Real admission must stay inside the deterministic CLI fixture. Its
        # native wipe verifier and host inventory cannot contact lab machines.
        wipe = self.root/'tests/helpers/lab-wipe-nodes.sh'
        wipe.write_text('#!/bin/bash\n[[ "$*" == "--verify --rocky-only 192.168.122.212" ]]\n')
        ssh = self.root/'kube-bin/ssh'
        ssh.write_text('#!'+sys.executable+'\nimport json,sys,time\nfrom pathlib import Path\naddress=next(a for a in sys.argv if a.startswith("root@"))[5:]\n'
            'print(json.dumps({"cpu":8,"memory":16*1024**3,"free":32*1024**3,"kernel":"5.14.0-fixture","arch":"x86_64","product_uuid":address,"time":time.time()}))\n')
        ssh.chmod(0o700)
        gate = (ROOT/'tests/helpers/elasticsearch-gate.sh').read_text().replace('BASE=/usr/local/kubeauto', 'BASE='+str(self.root)).replace('/root/.kube', str(self.root/'.kube')).replace('/var/tmp/kubeauto-elasticsearch-module.', str(self.root/'work/module.'))
        self.gate = self.root/'gate.sh'; self.gate.write_text(gate)
        self.responses = {'namespace': {'metadata': {'uid': 'cd102ec9-acd8-43f3-adb6-5f10bca24059'}},
                          'nodes': {'items': [{'status': {'addresses': [{'type': 'InternalIP', 'address': ip}], 'nodeInfo': {'kubeletVersion': 'v1.37.1'}}} for ip in LAB.PREVIOUS_HOSTS]},
                          'namespaces': {'items': [{'metadata': {'name': name}} for name in ('default', 'kube-system', 'kube-public', 'kube-node-lease')]}}

    def run_gate(self, module):
        import os
        (self.root/'responses.json').write_text(json.dumps(self.responses))
        prefix = self.root/'durable'
        result = subprocess.run(['bash', str(ROOT/'tests/helpers/run-durable-gate.sh'), str(prefix), 'ELASTICSEARCH_GATE_EXIT', 'bash', str(self.gate), module],
                                env=dict(os.environ, ELASTICSEARCH_PARENT_LEASE_HELD='yes'), text=True, capture_output=True, timeout=20)
        self.assertEqual(str(result.returncode), prefix.with_suffix('.exit').read_text().strip())
        self.assertEqual(str(result.returncode), prefix.with_suffix('.finalized').read_text().strip())
        self.assertEqual(b'preserve original context\n', self.context.read_bytes())
        self.assertEqual(0o600, self.context.stat().st_mode & 0o777)
        return result

    def test_real_admission_preconditions_then_context_restore_then_durable_fence(self):
        result = self.run_gate('admission')
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertLess(next(i for i,v in enumerate(lines) if v.startswith('ELASTICSEARCH_SOURCE_ADMISSION_PASS')), lines.index('ELASTICSEARCH_CONTROL_CONTEXT_RESTORED module=admission'))
        self.assertLess(lines.index('ELASTICSEARCH_MODULE_PASS module=admission'), lines.index('ELASTICSEARCH_GATE_EXIT rc=0'))
        self.assertFalse((self.root/'state/ownership.json').exists())
        self.assertEqual([], list((self.root/'work').iterdir()))

    def test_real_bootstrap_rejects_changed_cluster_before_any_owned_mutation(self):
        self.responses['namespace']['metadata']['uid'] = 'changed-cluster'
        result = self.run_gate('bootstrap')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('changed since', result.stderr)
        self.assertNotIn('ELASTICSEARCH_MODULE_PASS', result.stdout)
        self.assertFalse((self.root/'state/ownership.json').exists())
        self.assertFalse((self.root/'clusters/elasticsearch-gate').exists())
        self.assertTrue(list((self.root/'work').glob('*/first-failure.rc')))

    def test_real_admission_consumes_empty_kubectl_stdout_without_list_json(self):
        stub = self.root/'kube-bin/kubectl'
        code = stub.read_text().replace('print(json.dumps(values.get(kind, {"items": []})))', 'print(json.dumps(values[kind]) if kind in values else "")')
        stub.write_text(code)
        result = self.run_gate('admission')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('ELASTICSEARCH_SOURCE_ADMISSION_PASS', result.stdout)

    def test_real_admission_refuses_persistent_customer_data(self):
        self.responses['pv'] = {'items': [{'metadata': {'name': 'customer-volume'}}]}
        result = self.run_gate('admission')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('persistent data', result.stderr)
        self.assertNotIn('ELASTICSEARCH_SOURCE_ADMISSION_PASS', result.stdout)

    def test_real_admission_rejects_duplicate_vm_identity_or_insufficient_memory_before_ownership(self):
        ssh = self.root/'kube-bin/ssh'
        original = ssh.read_text()
        for old, new, reason in (('"product_uuid":address', '"product_uuid":"duplicate"', 'distinct VM hardware'),
                                 ('"memory":16*1024**3', '"memory":4*1024**3', 'capacity, kernel')):
            with self.subTest(reason=reason):
                ssh.write_text(original.replace(old, new))
                result = self.run_gate('admission')
                self.assertNotEqual(0, result.returncode)
                self.assertIn(reason, result.stderr)
                self.assertFalse((self.root/'state/ownership.json').exists())


class SysctlRollbackContracts(unittest.TestCase):
    def setUp(self):
        import os
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root/'etc/sysctl.d').mkdir(parents=True)
        (self.root/'etc/machine-id').write_text('owned-machine')
        self.path = self.root/'etc/sysctl.d/99-kubeauto-elasticsearch.conf'
        self.runtime = 65530
        def run(argv, **kwargs):
            if argv[:2] == ['sysctl', '-n']:
                return subprocess.CompletedProcess(argv, 0, str(self.runtime)+'\n', '')
            if argv[:2] == ['sysctl', '-w']:
                self.runtime = int(argv[2].split('=')[1])
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError(argv)
        p = patch('subprocess.run', side_effect=run); p.start(); self.addCleanup(p.stop)

    def mutate(self, record):
        import base64
        self.path.write_bytes(base64.b64decode(record['expected_data']))
        self.runtime = record['expected_runtime']

    def test_absent_file_and_runtime_restore_exactly(self):
        record = MODULE.host_sysctl(root=self.root)
        self.mutate(record)
        self.assertTrue(MODULE.host_sysctl(record, 'restore', self.root)['restored'])
        self.assertFalse(self.path.exists()); self.assertEqual(65530, self.runtime)

    def test_existing_higher_file_and_mode_are_preserved(self):
        self.path.write_text('# customer context\nvm.max_map_count=2097152\n'); self.path.chmod(0o640)
        prior = self.path.read_bytes()
        record = MODULE.host_sysctl(root=self.root)
        self.assertEqual(2097152, record['expected_runtime'])
        self.mutate(record)
        MODULE.host_sysctl(record, 'restore', self.root)
        self.assertEqual(prior, self.path.read_bytes()); self.assertEqual(0o640, self.path.stat().st_mode & 0o777)
        self.assertEqual(65530, self.runtime)

    def test_foreign_host_file_or_runtime_refuses_rollback(self):
        for defect in ('host', 'file', 'runtime'):
            record = MODULE.host_sysctl(root=self.root); self.mutate(record)
            if defect == 'host': record['machine_id'] = 'other'
            elif defect == 'file': self.path.write_text('# administrator replacement\n')
            else: self.runtime += 1
            saved = self.path.read_bytes()
            with self.subTest(defect=defect), self.assertRaises(ValueError): MODULE.host_sysctl(record, 'restore', self.root)
            self.assertEqual(saved, self.path.read_bytes())
            self.path.unlink(); self.runtime = 65530

    def test_failed_install_without_mutation_has_idempotent_cleanup(self):
        record = MODULE.host_sysctl(root=self.root)
        self.assertTrue(MODULE.host_sysctl(record, 'restore', self.root)['restored'])

    def test_interrupted_restore_resumes_after_only_one_side_was_restored(self):
        for restored in ('file', 'runtime'):
            record = MODULE.host_sysctl(root=self.root)
            self.mutate(record)
            if restored == 'file': self.path.unlink()
            else: self.runtime = record['runtime']
            with self.subTest(restored=restored):
                self.assertTrue(MODULE.host_sysctl(record, 'restore', self.root)['restored'])
            self.assertFalse(self.path.exists()); self.assertEqual(65530, self.runtime)

    def test_product_generated_shell_executes_and_never_lowers_runtime_or_persistence(self):
        import os
        from tests.unit.test_elasticsearch_delivery import es, configuration
        # Capture the actual product-generated stdin, then execute its shell
        # using only a sysctl fixture; no SSH or host /etc writes occur.
        scripts = []
        def capture(argv, **kwargs):
            scripts.append(kwargs['input']); return subprocess.CompletedProcess(argv, 0, '', '')
        api = SimpleNamespace(get=lambda *a: {'status': {'addresses': [{'type': 'InternalIP', 'address': '192.168.122.243'}]}})
        with patch.object(es.subprocess, 'run', side_effect=capture): es.sysctl(configuration('compact'), api)
        self.assertEqual(3, len(scripts))
        script = scripts[0].replace('/etc/sysctl.d', str(self.root/'etc/sysctl.d'))
        (self.root/'runtime').write_text('2097152')
        stub = self.root/'sysctl'
        stub.write_text('#!/bin/sh\nif [ "$1" = -n ]; then cat "'+str(self.root/'runtime')+'"; else printf "%s" "${2#*=}" > "'+str(self.root/'runtime')+'"; fi\n')
        stub.chmod(0o700)
        # subprocess.Popen is independent from the patched transport run().
        process = subprocess.Popen(['bash', '-s'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=dict(os.environ, PATH=str(self.root)+':'+os.environ['PATH']))
        output, errors = process.communicate(script, timeout=10)
        self.assertEqual(0, process.returncode, errors)
        self.assertEqual('vm.max_map_count=2097152\n', self.path.read_text())
        self.assertEqual('2097152', (self.root/'runtime').read_text())


class BoundedClientWriteContracts(unittest.TestCase):
    def setUp(self):
        class Timeout(Exception): pass
        class APIError(Exception): pass
        self.Timeout = Timeout
        p = patch.dict(sys.modules, {'elastic_transport': SimpleNamespace(ConnectionTimeout=Timeout, ConnectionError=Timeout),
                                    'elasticsearch': SimpleNamespace(ApiError=APIError)})
        p.start(); self.addCleanup(p.stop)

    def test_soak_incomplete_shard_acknowledgement_requires_matching_readback_before_confirmation(self):
        for shards in ({'successful': 1, 'failed': 1}, {'successful': 0, 'failed': 0}):
            counters = {'acknowledged_copies': {}, 'rejected_or_conflict': 0, 'unknown_outcomes': 0,
                        'confirmed_by_readback': 0, 'readback_unavailable': 0, 'retries': 0,
                        'incomplete_shard_responses': 0}
            client = Mock()
            client.perform_request.side_effect = [
                SimpleNamespace(meta=SimpleNamespace(status=201), body={'_shards': shards}),
                SimpleNamespace(meta=SimpleNamespace(status=200), body={'found': True, '_source': {'x': 1}})]
            with self.subTest(shards=shards), patch.object(BUSINESS.time, 'sleep') as sleep:
                BUSINESS.confirmed_create(client, 'owned', 'stable', {'x': 1}, counters)
            self.assertEqual(['PUT', 'GET'], [c.args[0] for c in client.perform_request.call_args_list])
            self.assertEqual(1, counters['incomplete_shard_responses'])
            self.assertEqual(1, counters['confirmed_by_readback'])
            self.assertEqual({}, counters['acknowledged_copies'])
            sleep.assert_not_called()

    def test_soak_incomplete_shard_responses_keep_the_budget_and_reject_changed_content(self):
        for observed in ({'found': False}, {'found': True, '_source': {'x': 2}}):
            counters = {'acknowledged_copies': {}, 'rejected_or_conflict': 0, 'unknown_outcomes': 0,
                        'confirmed_by_readback': 0, 'readback_unavailable': 0, 'retries': 0,
                        'incomplete_shard_responses': 0}
            client = Mock()
            pair = [SimpleNamespace(meta=SimpleNamespace(status=201), body={'_shards': {'successful': 1, 'failed': 1}}),
                    SimpleNamespace(meta=SimpleNamespace(status=200 if observed['found'] else 404), body=observed)]
            client.perform_request.side_effect = pair * 40
            with self.subTest(observed=observed), patch.object(BUSINESS.time, 'sleep'), self.assertRaisesRegex(
                    ValueError, 'content differs' if observed['found'] else 'exhausted'):
                BUSINESS.confirmed_create(client, 'owned', 'stable', {'x': 1}, counters)
            self.assertEqual(2 if observed['found'] else 80, client.perform_request.call_count)
            self.assertEqual(0, counters['confirmed_by_readback'])
            self.assertEqual({}, counters['acknowledged_copies'])

    def test_soak_write_can_outlast_six_admission_refusals_but_stops_at_its_deadline(self):
        def counters():
            return {'acknowledged_copies': {}, 'incomplete_shard_responses': 0,
                    'rejected_or_conflict': 0, 'unknown_outcomes': 0, 'confirmed_by_readback': 0,
                    'readback_unavailable': 0, 'retries': 0}
        refused=[SimpleNamespace(meta=SimpleNamespace(status=503),body={'error':{'type':'unavailable_shards_exception'}}),
                 SimpleNamespace(meta=SimpleNamespace(status=404),body={'found':False})]
        client=Mock();client.perform_request.side_effect=refused*7+[
            SimpleNamespace(meta=SimpleNamespace(status=201),body={'_shards':{'successful':2,'failed':0}})]
        ledger=counters()
        with patch.object(BUSINESS.time,'sleep'),patch.object(BUSINESS.time,'monotonic',return_value=0):
            BUSINESS.confirmed_create(client,'owned','stable',{'x':1},ledger)
        self.assertEqual(7,ledger['retries']);self.assertEqual({'2':1},ledger['acknowledged_copies'])
        self.assertEqual(15,client.perform_request.call_count)
        client=Mock();client.perform_request.side_effect=refused;ledger=counters()
        with patch.object(BUSINESS.time,'sleep'),patch.object(BUSINESS.time,'monotonic',side_effect=[0,0,301]),self.assertRaisesRegex(ValueError,'exhausted'):
            BUSINESS.confirmed_create(client,'owned','stable',{'x':1},ledger)
        self.assertEqual(2,client.perform_request.call_count)

    def test_soak_search_recovers_connection_and_partial_response_without_accepting_partial_data(self):
        counters = {'search_rejected_or_partial': 0, 'search_unavailable': 0, 'search_retries': 0}
        good = {'_shards': {'failed': 0, 'total': 3}, 'hits': {'hits': []}}
        client = Mock()
        client.perform_request.side_effect = [self.Timeout(),
            SimpleNamespace(meta=SimpleNamespace(status=200), body={'_shards': {'failed': 1, 'total': 3}}),
            SimpleNamespace(meta=SimpleNamespace(status=200), body=good)]
        with patch.object(BUSINESS.time, 'sleep') as sleep:
            self.assertEqual(good, BUSINESS.bounded_search(client, 'owned', counters))
        self.assertEqual({'search_rejected_or_partial': 1, 'search_unavailable': 1, 'search_retries': 2}, counters)
        self.assertEqual([((.25,), {}), ((.5,), {})], sleep.call_args_list)
        for call in client.perform_request.call_args_list:
            self.assertEqual({'allow_partial_search_results': 'false'}, call.kwargs['params'])

    def test_soak_search_stops_at_its_budget_and_does_not_retry_authorization_errors(self):
        for status in (429, 503, 403):
            counters = {'search_rejected_or_partial': 0, 'search_unavailable': 0, 'search_retries': 0}
            client = Mock()
            client.perform_request.return_value = SimpleNamespace(meta=SimpleNamespace(status=status), body={'error': {'type': 'fixture'}})
            with patch.object(BUSINESS.time, 'sleep'), self.assertRaises(ValueError):
                BUSINESS.bounded_search(client, 'owned', counters)
            self.assertEqual(1 if status == 403 else 6, client.perform_request.call_count)

    def test_rejected_write_has_exact_attempt_budget_and_exponential_backoff(self):
        client = Mock()
        client.perform_request.return_value = SimpleNamespace(meta=SimpleNamespace(status=429), body={'error': {'type': 'es_rejected_execution_exception'}})
        with patch.object(BUSINESS.time, 'sleep') as sleep, self.assertRaisesRegex(ValueError, 'exhausted'):
            BUSINESS.bounded_create(client, 'owned', 'stable', {'x': 1})
        self.assertEqual(3, client.perform_request.call_count)
        self.assertEqual([((.1,), {}), ((.2,), {})], sleep.call_args_list)

    def test_unknown_write_reads_stable_id_without_replay(self):
        client = Mock()
        client.perform_request.side_effect = [self.Timeout(), SimpleNamespace(meta=SimpleNamespace(status=200), body={'found': True, '_source': {'x': 1}})]
        result = BUSINESS.bounded_create(client, 'owned', 'stable', {'x': 1})
        self.assertTrue(result['unknown_outcome'])
        self.assertEqual(['PUT', 'GET'], [c.args[0] for c in client.perform_request.call_args_list])

    def test_unknown_unconfirmed_or_conflicting_write_stops(self):
        for data in ({'found': False}, {'found': True, '_source': {'x': 2}}):
            client = Mock()
            client.perform_request.side_effect = [self.Timeout(), SimpleNamespace(meta=SimpleNamespace(status=200), body=data)]
            with self.subTest(data=data), self.assertRaises(ValueError): BUSINESS.bounded_create(client, 'owned', 'stable', {'x': 1})
            self.assertEqual(2, client.perform_request.call_count)


class SecurityRotationContracts(unittest.TestCase):
    def test_tls_refusal_uses_certificate_cause_when_transport_clears_error_text(self):
        import ssl
        from urllib3.util.ssl_match_hostname import CertificateError
        class TransportConnectionError(Exception):
            pass
        class TlsError(TransportConnectionError):
            def __str__(self):
                return 'TLS error'
        with patch.dict(sys.modules, {'elastic_transport': SimpleNamespace(TlsError=TlsError)}):
            for cause in (ssl.SSLCertVerificationError('untrusted CA'), CertificateError('wrong SAN')):
                def call(*args):
                    try:
                        raise cause
                    except Exception as original:
                        raise TlsError() from original
                with self.subTest(cause=type(cause).__name__), patch.object(BUSINESS, 'call', side_effect=call):
                    BUSINESS.certificate_refusal(None)
            for failure in (TransportConnectionError('certificate endpoint DNS failed'), ConnectionRefusedError()):
                with self.subTest(error=type(failure).__name__), patch.object(BUSINESS, 'call', side_effect=failure):
                    with self.assertRaises(type(failure)):
                        BUSINESS.certificate_refusal(None)
            with patch.object(BUSINESS, 'call', side_effect=TlsError()), self.assertRaisesRegex(ValueError, 'certificate validation'):
                BUSINESS.certificate_refusal(None)
            with patch.object(BUSINESS, 'call', return_value={}), self.assertRaisesRegex(ValueError, 'accepted'):
                BUSINESS.certificate_refusal(None)

    def test_client_failure_reports_owned_code_locations_without_exception_secrets(self):
        output = StringIO()
        try:
            try:
                raise ConnectionRefusedError('private-response')
            except ConnectionRefusedError:
                BUSINESS.require(False, 'encoded-api-key-and-private-response')
        except ValueError as error:
            with redirect_stderr(output):
                BUSINESS.report_failure(error)
        self.assertRegex(output.getvalue(), r'ELASTICSEARCH_BUSINESS_FAIL reason=ValueError location=require:[0-9]+')
        self.assertNotIn('encoded-api-key', output.getvalue())
        self.assertNotIn('private-response', output.getvalue())
        self.assertNotIn(str(ROOT), output.getvalue())
        self.assertIn('causes=ConnectionRefusedError', output.getvalue())

    def test_manage_own_key_rotation_and_cleanup_explicitly_limit_revocation_to_owner(self):
        import os
        import socket
        import ssl
        class TlsError(Exception):
            pass
        admin, limited, wrong, old, new, bad_ca, bad_host = [Mock() for _ in range(7)]
        created, revoked, deletions = [], set(), []
        def call(client, method, path, body=None, *, expected=(200, 201)):
            if client in (bad_ca, bad_host):
                raise TlsError('TLS error') from ssl.SSLCertVerificationError('certificate verification failed')
            if path == '/_security/api_key':
                self.assertIs(limited, client)
                if method == 'POST':
                    key = 'old' if not created else 'new'
                    created.append(key)
                    return {'id': key, 'encoded': key}
                # ES 9.5.6 manage_own_api_key rejects ids alone for native users.
                if body.get('owner') is not True:
                    raise ValueError('HTTP 403 security_exception')
                self.assertEqual({'ids', 'owner'}, set(body))
                deletions.append(list(body['ids']))
                revoked.update(body['ids'])
                return {'invalidated_api_keys': body['ids'], 'error_count': 0}
            if path.startswith('/_cluster/health/'):
                return {'status': 'green'}
            if path.startswith('/_nodes/settings'):
                return {'nodes': {'member': {'settings': {'xpack.security.transport.ssl.enabled': 'true'}}}}
            if method == 'PUT' and '/_doc/' in path:
                return {'_shards': {'failed': 0, 'successful': 2, 'total': 2}}
            if client is old and expected == (401,):
                self.assertIn('old', revoked)
            if client is new:
                self.assertNotIn('new', revoked)
            return {'found': True}
        with (patch.dict(sys.modules, {'elastic_transport': SimpleNamespace(TlsError=TlsError)}),
              patch.dict(os.environ, {'ELASTICSEARCH_URL': 'https://search.es-standard.svc:9200'}),
              patch.object(socket, 'gethostbyname', return_value='172.30.1.9'),
              patch.object(BUSINESS.Path, 'read_text', side_effect=['es-gate-limited', 'fixture-password']),
              patch.object(BUSINESS, 'connect', side_effect=[limited, wrong, old, new, bad_ca, bad_host]),
              patch.object(BUSINESS, 'call', side_effect=call)):
            result = BUSINESS.security(admin, 'es-gate-'+'a'*16)
        self.assertEqual(['ES-24', 'ES-25'], result['case_ids'])
        self.assertEqual([['old'], ['old', 'new']], deletions)
        for client in (limited, wrong, old, new, bad_ca, bad_host):
            client.close.assert_called_once()

    def test_safety_client_requires_the_specific_master_and_write_rejection(self):
        import os
        env = {'ELASTICSEARCH_FAULT_INDEX': 'es-gate-confirmed-'+'a'*32, 'ELASTICSEARCH_FAULT_KIND': 'quorum',
               'ELASTICSEARCH_ASSERT_HOSTNAME': 'search-client.es-standard.svc'}
        with patch.dict(os.environ, env), patch.object(BUSINESS.time, 'sleep'), patch.object(BUSINESS, 'call', side_effect=[{'status': 'green'},
            {'error': {'type': 'master_not_discovered_exception'}}, {'error': {'type': 'cluster_block_exception'}}]) as call:
            result = BUSINESS.unavailable_write(None, 'es-gate-'+'b'*16)
        self.assertEqual(503, result['write_http_status'])
        self.assertEqual((503,), call.call_args.kwargs['expected'])
        self.assertIn('/_create/es-gate-', call.call_args.args[2])
        with patch.dict(os.environ, env), patch.object(BUSINESS, 'call', return_value={'error': {'type': 'security_exception'}}), self.assertRaisesRegex(ValueError, 'master unavailability'):
            BUSINESS.unavailable_write(None, 'es-gate-'+'b'*16)

    def test_safety_probe_uses_the_pod_address_with_positive_service_hostname_validation(self):
        c = {'elasticsearch_namespace': 'es-standard', 'elasticsearch_name': 'search', 'elasticsearch_admin_secret': '',
             'elasticsearch_http_tls_secret': '', 'probe_ip': '172.30.1.9', 'probe_index': 'es-gate-confirmed-'+'a'*32, 'probe_fault': 'quorum'}
        with patch.object(LAB, 'BASE', ROOT), patch.object(LAB, 'load_es', return_value=SimpleNamespace(LABELS={})):
            cm, job = LAB.client_objects(c, 'unavailable-write', 'a'*16)
        env = {v['name']: v['value'] for v in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual('https://172.30.1.9:9200', env['ELASTICSEARCH_URL'])
        self.assertEqual('search-client.es-standard.svc', env['ELASTICSEARCH_ASSERT_HOSTNAME'])
        self.assertNotIn('verify_certs=False', cm['data']['run.py'])

    def test_network_job_has_its_own_namespace_and_public_ca_without_credentials(self):
        c = {'elasticsearch_namespace': 'es-standard', 'elasticsearch_name': 'search', 'elasticsearch_admin_secret': '',
             'elasticsearch_http_tls_secret': '', 'probe_namespace': 'es-client-standard', 'probe_expected': 'denied'}
        with patch.object(LAB, 'BASE', ROOT), patch.object(LAB, 'load_es', return_value=SimpleNamespace(LABELS={})):
            cm, job = LAB.client_objects(c, 'network', 'a'*16)
        self.assertEqual('es-client-standard', cm['metadata']['namespace'])
        self.assertEqual('es-client-standard', job['metadata']['namespace'])
        pod = job['spec']['template']['spec']
        self.assertFalse(any('secret' in v for v in pod['volumes']))
        env = {v['name']: v['value'] for v in pod['containers'][0]['env']}
        self.assertEqual('https://search-client.es-standard.svc:9200', env['ELASTICSEARCH_URL'])
        self.assertEqual('denied', env['ELASTICSEARCH_NETWORK_EXPECTED'])

    def test_network_deny_waits_for_two_drops_and_never_accepts_dns_or_refusal_errors(self):
        from unittest.mock import MagicMock
        url = 'https://search-client.es-standard.svc:9200'
        with patch.object(NETWORK.socket, 'getaddrinfo', return_value=[('resolved',)]), patch.object(NETWORK.time, 'sleep'):
            with patch.object(NETWORK.socket, 'create_connection', side_effect=[MagicMock(), TimeoutError(), TimeoutError()]) as create:
                NETWORK.probe(url, '/public-ca', 'denied')
                self.assertEqual(3, create.call_count)
            for error in (ConnectionRefusedError(), NETWORK.socket.gaierror()):
                with patch.object(NETWORK.socket, 'create_connection', side_effect=error), self.assertRaises(type(error)):
                    NETWORK.probe(url, '/public-ca', 'denied')
            with patch.object(NETWORK.socket, 'create_connection', return_value=MagicMock()), patch.object(NETWORK.time, 'monotonic', side_effect=[0, 1, 46]), self.assertRaisesRegex(ValueError, 'did not converge'):
                NETWORK.probe(url, '/public-ca', 'denied')

    def test_real_generated_certificate_chain_covers_both_services_and_wrong_san_is_refused(self):
        spec = importlib.util.spec_from_file_location('es_rotation_product', ROOT/'roles/cluster-addon/files/elasticsearch.py')
        es = importlib.util.module_from_spec(spec); spec.loader.exec_module(es)
        c = {'elasticsearch_name': 'search', 'elasticsearch_namespace': 'es-standard'}
        first, second = LAB.certificate_data(c), LAB.certificate_data(c)
        es.verify_http_certificate(c, {'data': first})
        import base64
        combined = base64.b64decode(first['ca.crt']) + b'\n' + base64.b64decode(second['ca.crt'])
        es.verify_http_certificate(c, {'data': second | {'ca.crt': base64.b64encode(combined).decode()}})
        with self.assertRaisesRegex(ValueError, 'chain, validity or service SAN'):
            es.verify_http_certificate(c, {'data': LAB.certificate_data(c, invalid_san=True)})
        with self.assertRaisesRegex(ValueError, 'chain, validity or service SAN'):
            es.verify_http_certificate(c, {'data': second | {'ca.crt': first['ca.crt']}})

    def test_expected_refusal_requires_both_nonzero_status_and_exact_product_cause(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(LAB, 'STATE', Path(temporary)):
            for rc, output in ((0, 'ELASTICSEARCH_REFUSED: SAN invalid'), (1, 'Ansible unavailable'), (1, 'ELASTICSEARCH_REFUSED: storage unavailable')):
                with patch.object(LAB.subprocess, 'run', return_value=subprocess.CompletedProcess([], rc, output, '')):
                    with self.assertRaisesRegex(ValueError, 'declared cause'): LAB.product_refusal('SAN invalid', 'san')
            with patch.object(LAB.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'ELASTICSEARCH_REFUSED: SAN invalid')):
                LAB.product_refusal('SAN invalid', 'san')
            self.assertEqual(0o600, (Path(temporary)/'expected-refusal-san.log').stat().st_mode & 0o777)

    def test_password_probe_requires_current_identity_and_old_password_unauthorized(self):
        import builtins
        current, previous = Mock(), Mock()
        observed = []
        def call(client, method, path, **kwargs):
            observed.append((client, kwargs.get('expected')))
            return {'username': 'limited'}
        with patch.object(BUSINESS.Path, 'read_text', side_effect=['limited', 'new-password', 'old-password']), patch.object(BUSINESS, 'connect', side_effect=[current, previous]) as connect, patch.object(BUSINESS, 'call', side_effect=call):
            BUSINESS.password_rotation(None, 'owned')
        self.assertEqual([(current, None), (previous, (401,)), (current, None)], observed)
        self.assertEqual(('limited', 'new-password'), connect.call_args_list[0].kwargs['basic_auth'])
        self.assertEqual(('limited', 'old-password'), connect.call_args_list[1].kwargs['basic_auth'])
        current.close.assert_called_once(); previous.close.assert_called_once()


class SecondaryLeaseContracts(unittest.TestCase):
    def test_real_guardian_retains_shared_locks_until_matching_source_durable_fence(self):
        import hashlib, os, time
        for source_rc in (0, 7):
            with self.subTest(source_rc=source_rc), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); (root/'bin').mkdir(); (root/'locks').mkdir()
                durable = ROOT/'tests/helpers/run-durable-gate.sh'
                code = (ROOT/'tests/helpers/elasticsearch-secondary-lease.sh').read_text()
                code = code.replace('/usr/local/kubeauto/tests/helpers/run-durable-gate.sh', str(durable))
                code = code.replace('/var/lock/', str(root/'locks')+'/').replace('/tmp/kubeauto-', str(root/'kubeauto-'))
                script = root/'secondary.sh'; script.write_text(code)
                ssh = root/'bin/ssh'
                ssh.write_text('#!/bin/bash\nif [[ "${@: -1}" == true ]]; then\n'
                    '  if [[ " $* " != *" -n "* ]]; then cat >/dev/null; fi\n  exit 0\nfi\n'
                    'exec bash -s -- "${@: -1}"\n'); ssh.chmod(0o700)
                sleep = root/'bin/sleep'; sleep.write_text('#!/bin/bash\nexec /bin/sleep 0.05\n'); sleep.chmod(0o700)
                run_id = 'a'*32
                live = root/'kubeauto-elasticsearch-live.log'
                live.write_text('ELASTICSEARCH_MODULE_BEGIN module=admission run_id='+'b'*32+'\n')
                for suffix in ('exit', 'finalized'): (root/('kubeauto-elasticsearch-gate.'+suffix)).write_text('0')
                result = subprocess.run(['bash', '-s', '--', run_id, hashlib.sha256(durable.read_bytes()).hexdigest()], input=code,
                    env=dict(os.environ, PATH=str(root/'bin')+':'+os.environ['PATH']), text=True, capture_output=True, timeout=5)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('ELASTICSEARCH_SECONDARY_LEASE_READY', result.stdout)
                deadline = time.monotonic()+3
                state = root/'kubeauto-elasticsearch-secondary.exit'
                try:
                    for suffix in ('ceph-priority', 'ceph-compute', 'redis-core-pool'):
                        lock = root/('locks/kubeauto-'+suffix+'.lock')
                        check = subprocess.run(['flock', '-n', str(lock), 'true'])
                        self.assertNotEqual(0, check.returncode, 'guardian released a shared lock before the matching source run')
                    # An old source exit must never unlock a new run's hosts.
                    self.assertFalse(state.exists())
                    for suffix in ('exit', 'finalized'): (root/('kubeauto-elasticsearch-gate.'+suffix)).unlink()
                    live.write_text('ELASTICSEARCH_MODULE_BEGIN module=admission run_id='+run_id+'\n')
                    (root/'kubeauto-elasticsearch-gate.exit').write_text(str(source_rc))
                    time.sleep(.1)
                    self.assertFalse(state.exists(), 'source exit without its finalized fence released the lease')
                finally:
                    live.write_text('ELASTICSEARCH_MODULE_BEGIN module=admission run_id='+run_id+'\n')
                    for suffix in ('exit', 'finalized'): (root/('kubeauto-elasticsearch-gate.'+suffix)).write_text(str(source_rc))
                    while not state.exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertEqual(str(source_rc), state.read_text().strip())
                for suffix in ('ceph-priority', 'ceph-compute', 'redis-core-pool'):
                    self.assertEqual(0, subprocess.run(['flock', '-n', str(root/('locks/kubeauto-'+suffix+'.lock')), 'true']).returncode)


class ScopedDeliveryPathContracts(unittest.TestCase):
    def test_real_launch_collection_preserves_private_child_directories_and_failed_parent(self):
        import os,shlex
        runner=(ROOT/'tests/helpers/elasticsearch-runner.sh').read_text()
        launch=runner.split('elasticsearch_launch() {',1)[1].split('\nelasticsearch_local_gate() {',1)[0]
        tail=launch[launch.index('  monitor_remote_job elasticsearch ssh_elasticsearch '):]
        for parent_rc in (0,1):
            with self.subTest(parent_rc=parent_rc),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);directory=root/'evidence';directory.mkdir(mode=0o700)
                children=directory/'children';children.mkdir(mode=0o700)
                checkpoint=children/'checkpoints';checkpoint.mkdir(mode=0o700)
                receipt=checkpoint/'receipt.json';receipt.write_text('immutable receipt\n');receipt.chmod(0o600)
                binary=root/'.venv/bin/python';binary.parent.mkdir(parents=True)
                binary.write_text('#!/bin/bash\nset -eu\n'
                    'test "$(stat -c %a "$3/children")" = 700\n'
                    'test "$(stat -c %a "$3/children/checkpoints")" = 700\n'
                    'printf "%s\\n" "$2" >> '+shlex.quote(str(root/'calls'))+'\n')
                binary.chmod(0o700)
                script='set -eu\nROOT="$1"\ndirectory="$ROOT/evidence"\nmodule=only\nmarker=full-pass\nrc=0\n'
                script+='monitor_remote_job() { if [[ "$1" == elasticsearch ]]; then return "$2"; fi; }\n'.replace('return "$2"','return '+str(parent_rc))
                script+='remote_job_summary() { printf summary; }\nssh_elasticsearch() { printf "0\\n"; }\nssh_elasticsearch_secondary() { printf "0\\n"; }\n'
                script+='finish_launch() {\n'+tail+'\nfinish_launch\n'
                result=subprocess.run(['bash','-c',script,'contract',str(root)],capture_output=True,text=True,timeout=10)
                self.assertEqual(parent_rc,result.returncode,result.stdout+result.stderr)
                self.assertEqual(0o700,children.stat().st_mode&0o777)
                self.assertEqual(0o700,checkpoint.stat().st_mode&0o777)
                self.assertEqual('immutable receipt\n',receipt.read_text())
                self.assertEqual(['recover-children']+(['publish'] if parent_rc==0 else []),(root/'calls').read_text().splitlines())
                for path in directory.iterdir():
                    if path.is_file():self.assertEqual(0o600,path.stat().st_mode&0o777)

    def test_collection_permission_fix_preserves_only_authenticated_unchanged_launch_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);name='tests/helpers/elasticsearch-runner.sh';path=root/name;path.parent.mkdir(parents=True)
            current=(ROOT/name).read_text()
            old='chmod 0600 "$directory"/*'
            fixed='find "$directory" -maxdepth 1 -type f -exec chmod 0600 {} +'
            original=current.replace(fixed,old);updated=current.replace(old,fixed)
            path.write_text(original);checksum=EVIDENCE.digest(path);directory=root/'logs/original'
            with patch.object(EVIDENCE,'launch_dependencies',return_value={name:checksum}):EVIDENCE.freeze(root,directory,'compact')
            receipt={'provenance':[EVIDENCE.binding(root,directory/'launch-sources.tar.gz')]}
            path.write_text(updated)
            self.assertTrue(MATRIX.dependency_matches(root,receipt,{'id':'ES-13'},name,checksum))
            for cid in ('ES-01','ES-59'):
                self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':cid},name,checksum))
            self.assertFalse(MATRIX.dependency_matches(root,{}, {'id':'ES-13'},name,checksum))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-13'},name,'0'*64))
            path.write_text(updated.replace('monitor_remote_job elasticsearch ssh_elasticsearch','monitor_remote_job changed ssh_elasticsearch'))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-13'},name,checksum))

    def test_documentation_dispatch_addition_preserves_live_launches_but_other_runner_changes_do_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);name='tests/helpers/elasticsearch-runner.sh';path=root/name;path.parent.mkdir(parents=True)
            current=(ROOT/name).read_text()
            arm='    --elasticsearch-documentation)\n      elasticsearch_local_gate documentation ELASTICSEARCH_DOCUMENTATION_SCOPED_CLEAN_PASS "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-full.py" --documentation "$ROOT" ;;\n'
            self.assertIn(arm,current);path.write_text(current.replace(arm,''));checksum=EVIDENCE.digest(path)
            directory=root/'logs/original'
            with patch.object(EVIDENCE,'launch_dependencies',return_value={name:checksum}):EVIDENCE.freeze(root,directory,'compact')
            receipt={'provenance':[EVIDENCE.binding(root,directory/'launch-sources.tar.gz')]}
            path.write_text(current)
            self.assertTrue(MATRIX.dependency_matches(root,receipt,{'id':'ES-13'},name,checksum))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-59'},name,checksum))
            self.assertFalse(MATRIX.dependency_matches(root,{}, {'id':'ES-13'},name,checksum))
            path.write_text(current.replace('elasticsearch_launch() {','elasticsearch_launch() {\n  return 1'))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-13'},name,checksum))

    def test_logging_runner_change_preserves_authenticated_non_logging_receipts(self):
        import shutil
        for name in ('tests/helpers/logging-regression.sh','tests/helpers/logging-artifact-gate.sh','tests/helpers/kafka-cleanup.sh'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);path=root/name;path.parent.mkdir(parents=True);shutil.copy(ROOT/name,path)
                checksum=EVIDENCE.digest(path);directory=root/'logs/original'
                with patch.object(EVIDENCE,'launch_dependencies',return_value={name:checksum}):
                    EVIDENCE.freeze(root,directory,'backup-source')
                receipt={'provenance':[EVIDENCE.binding(root,directory/'launch-sources.tar.gz')]}
                path.write_text(path.read_text()+'\n# changed logging-only launch\n')
                for cid in ('ES-39','ES-43','ES-50','ES-56'):
                    self.assertTrue(MATRIX.dependency_matches(root,receipt,{'id':cid},name,checksum))
                self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-55'},name,checksum))
                self.assertFalse(MATRIX.dependency_matches(root,{}, {'id':'ES-39'},name,checksum))
                self.assertNotIn(name,EVIDENCE.dependencies(ROOT,'backup-source'))
                self.assertIn(name,EVIDENCE.dependencies(ROOT,'logging'))

    def test_soak_only_client_change_preserves_authenticated_non_soak_evidence(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);name='tests/helpers/elasticsearch-business.py';path=root/name
            path.parent.mkdir(parents=True);shutil.copy(ROOT/name,path)
            original=path.read_text();checksum=EVIDENCE.digest(path);directory=root/'logs/original'
            with patch.object(EVIDENCE,'launch_dependencies',return_value={name:checksum}):
                EVIDENCE.freeze(root,directory,'backup-source')
            receipt={'provenance':[EVIDENCE.binding(root,directory/'launch-sources.tar.gz')]}
            changed=original.replace('def confirmed_create(client, index, identifier, document, counters):',
                'def confirmed_create(client, index, identifier, document, counters):\n    raise ValueError("changed soak write")')
            changed=changed.replace('def soak(client, prefix):','def soak(client, prefix):\n    raise ValueError("changed soak loop")')
            path.write_text(changed)
            for number in range(39,48):
                self.assertTrue(MATRIX.dependency_matches(root,receipt,{'id':f'ES-{number}'},name,checksum))
            for cid in ('ES-01','ES-48','ES-49'):
                self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':cid},name,checksum))
            self.assertFalse(MATRIX.dependency_matches(root,{}, {'id':'ES-39'},name,checksum))
            path.write_text(changed.replace('def call(client, method, path, body=None, *, expected=(200, 201), ndjson=False):',
                'def call(client, method, path, body=None, *, expected=(200, 201), ndjson=False):\n    raise ValueError("changed shared API")'))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-39'},name,checksum))

    def test_archived_inputs_prove_unaffected_cases_without_rewriting_receipts(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            names=('roles/cluster-addon/files/elasticsearch.py','tests/helpers/elasticsearch-dr.py',
                   'tests/helpers/elasticsearch-evidence.py','tests/helpers/validate_elasticsearch_matrix.py',
                   'tests/helpers/elasticsearch-stop.py')
            for name in names:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);shutil.copy(ROOT/name,path)
            original={name:(root/name).read_text() for name in names}
            inputs={name:EVIDENCE.digest(root/name) for name in names}
            directory=root/'logs/original'
            with patch.object(EVIDENCE,'launch_dependencies',return_value=inputs):EVIDENCE.freeze(root,directory,'maintenance')
            receipt={'provenance':[EVIDENCE.binding(root,directory/'launch-sources.tar.gz')]}
            unchanged=json.dumps(receipt,sort_keys=True)
            product=root/names[0]
            product.write_text(original[names[0]].replace('--no-es.aliases','--es.aliases=false').replace('or absent(elasticsearch_cluster_health_status{{{selector}}})','or (elasticsearch_cluster_health_up{{{selector}}} == 0)'))
            for cid in ('ES-35','ES-39','ES-40','ES-41','ES-42','ES-43'):
                self.assertTrue(MATRIX.dependency_matches(root,receipt,{'id':cid},names[0],inputs[names[0]]))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-46'},names[0],inputs[names[0]]))
            product.write_text(product.read_text().replace('def enable_monitoring(c, api):','def enable_monitoring(c, api):\n    raise ValueError("changed monitoring gate")'))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-35'},names[0],inputs[names[0]]))
            product.write_text(original[names[0]].replace('def index_uuids(request, indices):','def index_uuids(request, indices):\n    raise ValueError("changed restore identity")'))
            for cid in ('ES-35','ES-39','ES-40'):
                self.assertTrue(MATRIX.dependency_matches(root,receipt,{'id':cid},names[0],inputs[names[0]]))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-41'},names[0],inputs[names[0]]))
            self.assertFalse(MATRIX.dependency_matches(root,{}, {'id':'ES-35'},names[0],inputs[names[0]]))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-35'},names[0],'0'*64))
            product.write_text(product.read_text().replace('def operation(c, api, request, req):','def operation(c, api, request, req):\n    raise ValueError("changed shared operation")'))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-39'},names[0],inputs[names[0]]))
            dr=root/names[1]
            dr.write_text(original[names[1]].replace('def features(source):','def features(source):\n    raise ValueError("changed feature fixture")'))
            self.assertTrue(MATRIX.dependency_matches(root,receipt,{'id':'ES-41'},names[1],inputs[names[1]]))
            self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-42'},names[1],inputs[names[1]]))
            # A historical aggregate case set may contain a file absent from
            # the actual focused launch. Prove that absence from frozen inputs.
            focused=dict(receipt,dependencies={},scoped_cleanup_marker='ELASTICSEARCH_MODULE_PASS module=backup-source')
            launch=directory/'focused-provenance.json';launch.write_text('{}')
            canonical=directory/'focused/launch-provenance.json';canonical.parent.mkdir();canonical.write_bytes(launch.read_bytes())
            focused['provenance']=[EVIDENCE.binding(root,canonical)]
            self.assertTrue(MATRIX.dependency_matches(root,focused,{'id':'ES-39'},names[1],inputs[names[1]]))
            canonical.write_text(json.dumps({names[1]:inputs[names[1]]}))
            focused['provenance']=[EVIDENCE.binding(root,canonical)]
            self.assertFalse(MATRIX.dependency_matches(root,focused,{'id':'ES-39'},names[1],inputs[names[1]]))
            for name in names[2:]:
                (root/name).write_text(original[name]+'\n# receipt processor revision\n')
                self.assertTrue(MATRIX.dependency_matches(root,receipt,{'id':'ES-35'},name,inputs[name]))
                self.assertFalse(MATRIX.dependency_matches(root,receipt,{'id':'ES-01'},name,inputs[name]))
            self.assertEqual(unchanged,json.dumps(receipt,sort_keys=True))
            archive=directory/'launch-sources.tar.gz';archive.write_bytes(archive.read_bytes()+b'corruption')
            with self.assertRaisesRegex(ValueError,'bytes differ'):
                MATRIX.dependency_matches(root,receipt,{'id':'ES-41'},names[1],inputs[names[1]])

    def test_restore_completion_change_preserves_only_authenticated_restore_free_consumers(self):
        name = 'roles/cluster-addon/files/elasticsearch.py'
        current = (ROOT/name).read_text()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root/name; path.parent.mkdir(parents=True)
            # A synthetic earlier restore implementation with the original
            # fixed 60-second transport. All unrelated product bytes match.
            previous = current.replace('def request(method, path, body=None, *, timeout=60):',
                                       'def request(method, path, body=None):').replace('port, timeout=timeout, context=context)',
                                                                                  'port, timeout=60, context=context)')
            previous = previous.replace('restoring = set(indices) | feature_indices',
                                        'raise ValueError("old restore completion")\n            restoring = set(indices) | feature_indices')
            path.write_text(previous); checksum = EVIDENCE.digest(path)
            directory = root/'logs/original'
            with patch.object(EVIDENCE, 'launch_dependencies', return_value={name:checksum}):
                EVIDENCE.freeze(root, directory, 'backup-source')
            receipt = {'provenance':[EVIDENCE.binding(root, directory/'launch-sources.tar.gz')]}
            path.write_text(current)
            for number in (7,8,9,10,13,15,21,24,26,28,29,30,35,39,40,46,47,48,49,50,53,55,56,57):
                with self.subTest(case=number):
                    self.assertTrue(MATRIX.dependency_matches(root, receipt, {'id':f'ES-{number:02}'}, name, checksum))
            for number in (1,41,42,43,44,45,51,52,54,59):
                with self.subTest(case=number):
                    self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':f'ES-{number:02}'}, name, checksum))
            for changed in (
                current.replace('*, timeout=60):', '*, timeout=61):'),
                current.replace('port, timeout=timeout, context=context)', 'port, timeout=timeout + 1, context=context)'),
                current.replace('if kind == "snapshot":', 'if kind != "restore":'),
                current.replace('def operation(c, api, request, req):', 'def operation(c, api, request, req):\n    raise ValueError("changed shared operation")'),
                current.replace('def connection(c, api):', 'def connection(c, api):\n    raise ValueError("changed shared transport")')):
                path.write_text(changed)
                self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':'ES-39'}, name, checksum))
            self.assertFalse(MATRIX.dependency_matches(root, {}, {'id':'ES-39'}, name, checksum))
            path.write_text(current.replace('def index_uuids(request, indices):',
                                            'def index_uuids(request, indices):\n    raise ValueError("changed restore identity")'))
            for number in (41,42,43,51,54):
                self.assertFalse(MATRIX.dependency_matches(root, receipt, {'id':f'ES-{number}'}, name, checksum))

    def test_feature_retry_does_not_reuse_a_snapshot_with_replaced_native_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary);(state/'ownership.json').write_text(json.dumps({'id':'same-source-owner'}))
            config={'elasticsearch_namespace':'es-standard','elasticsearch_name':'search',
                    'elasticsearch_kibana_enabled':True,'elasticsearch_s3_ca_configmap':'s3-ca',
                    'elasticsearch_repository_role':'writer'}
            fixtures={};attempts=[];dashboards={};requests=[]
            stale='es-feature-samesourceow-0123456789ab'
            fixtures[stale]={'metadata':{'name':stale,'uid':'failed-preflight-secret','labels':{'owned':'yes'}}}
            def get(kind,name='',namespace=''):
                if kind=='secrets':return {'items':list(fixtures.values())}
                if kind=='configmap':return {'metadata':{'uid':'ca'},'data':{'ca.crt':'fixture'}}
                return fixtures.get(name) or ({'metadata':{'uid':name},'data':{}} if name.startswith('search-es-') else None)
            def apply(objects):
                for obj in objects:fixtures[obj['metadata']['name']]=dict(obj,metadata=dict(obj['metadata'],uid=obj['metadata']['name']))
            def run(*args,**kwargs):fixtures.pop(args[4])
            api=SimpleNamespace(get=get,apply=apply,run=run)
            spec=importlib.util.spec_from_file_location('feature_product_contract',ROOT/'roles/cluster-addon/files/elasticsearch.py')
            product_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(product_module)
            def selected(topology,**updates):
                if updates.get('elasticsearch_requests'):
                    for req in updates['elasticsearch_requests']:product_module.validate_request(config,req)
                    requests.extend(updates['elasticsearch_requests'])
                return dict(config,**updates)
            def product(*args,**kwargs):
                if phase=='preflight':raise ValueError('injected preflight failure')
                if requests[-1]['type']=='snapshot':
                    attempts.append(dict(requests[-1]));raise ValueError('injected snapshot failure')
            def kibana(lab,c,method,path,body=None,**kwargs):
                if method=='POST':dashboards[path]={'id':path.rsplit('/',1)[1],'attributes':body['attributes']}
                if method=='DELETE':return dashboards.pop(path)
                return dashboards.get(path)
            @contextmanager
            def connection(*args):yield lambda *args: {}
            source=SimpleNamespace(selected_config=selected,api=lambda:api,load_es=lambda:SimpleNamespace(connection=connection),
                atomic=lambda path,value:path.write_text(json.dumps(value)),data_ledger=lambda topology:{'index':'orders'},
                product=product,NAME='source',LABELS={'owned':'yes'})
            with patch.object(DR,'SOURCE',state),patch.object(DR,'target_lab',return_value=object()),patch.object(DR,'baseline'),patch.object(DR,'kibana_request',side_effect=kibana):
                for phase in ('preflight','snapshot','snapshot'):
                    with self.assertRaisesRegex(ValueError,'injected '+phase):DR.features(source)
                    self.assertFalse(fixtures);self.assertFalse(dashboards)
            self.assertEqual(2,len(attempts));self.assertNotEqual(attempts[0]['snapshot'],attempts[1]['snapshot'])
            self.assertNotEqual(attempts[0]['id'],attempts[1]['id'])
            for attempt in attempts:
                name=attempt['snapshot'];self.assertLessEqual(len(name+'-snapshot'),40)
                self.assertTrue(any(req['type']=='user' and req['name']==name and req['password_secret']==name for req in requests))
                self.assertEqual(name+'-snapshot',attempt['id'])
            self.assertFalse(fixtures);self.assertFalse(dashboards)

    def test_feature_source_cleanup_refuses_changed_identity_and_handles_absent_native_objects(self):
        for defect in ('absent','complete','secret','uid','user','role','dashboard'):
            with self.subTest(defect=defect):
                name='es-feature-owner-0123456789ab';writes=[]
                secret={'metadata':{'name':name,'uid':'fixture','labels':{'owned':'yes'}}}
                current=copy.deepcopy(secret)
                if defect=='secret':current['metadata']['labels']={}
                if defect=='uid':current['metadata']['uid']='replacement'
                users={} if defect=='absent' else {name:{'roles':['foreign'] if defect=='user' else [name]}}
                roles={} if defect=='absent' else {name:{'cluster':[],'indices':[{'names':['orders'],'privileges':['all'] if defect=='role' else ['read']}]}}
                attributes={'title':'owned dashboard'}
                observed=None if defect=='absent' else {'attributes':{'title':'foreign' if defect=='dashboard' else 'owned dashboard'}}
                api=SimpleNamespace(get=lambda *args:current,run=lambda *args,**kwargs:writes.append('secret'))
                def request(method,path):
                    if method=='GET':return users if path.endswith('/user') else roles
                    writes.append(path)
                @contextmanager
                def connection(*args):yield request
                source=SimpleNamespace(api=lambda:api,load_es=lambda:SimpleNamespace(connection=connection),
                    LABELS={'owned':'yes'},data_ledger=lambda *args:{'index':'orders'})
                def kibana(*args,**kwargs):
                    if args[2]=='DELETE':writes.append('dashboard')
                    return observed
                with patch.object(DR,'kibana_request',side_effect=kibana):
                    if defect in ('absent','complete'):
                        DR.cleanup_feature_source(source,{'elasticsearch_namespace':'es-standard'},secret,attributes)
                        self.assertEqual(['secret'] if defect=='absent' else ['dashboard','/_security/user/'+name,'/_security/role/'+name,'secret'],writes)
                    else:
                        with self.assertRaises(ValueError):DR.cleanup_feature_source(source,{'elasticsearch_namespace':'es-standard'},secret,attributes)
                        self.assertFalse(writes)

    def test_current_contract_reuse_requires_every_case_and_original_raw_fenced_evidence(self):
        import shutil
        import yaml
        from contextlib import redirect_stdout
        fixture=MatrixEvidenceContracts()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        root=fixture.root
        (root/'tests/helpers').mkdir()
        shutil.copy(ROOT/'tests/helpers/validate_elasticsearch_matrix.py',root/'tests/helpers')
        record,path=fixture.evidence()
        cases=fixture.data['cases'][:4]
        for case in cases:case.update(status='pass',evidence=['tests/evidence.json'])
        record['case_ids']=[case['id'] for case in cases]
        record['raw_logs']=[fixture.binding('logs/raw.log','\n'.join(case['success_marker'] for case in cases)+'\nELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS\n')]
        matrix=root/'tests/elasticsearch-test-matrix.yaml'
        for defect in ('none','pending','missing-case','source-changed','fence','raw-log'):
            with self.subTest(defect=defect):
                current=copy.deepcopy(cases)
                (root/'product.py').write_text('# pinned production input\n')
                if defect=='pending':current[-1]['status']='pending'
                if defect=='missing-case':current.pop()
                if defect=='source-changed':(root/'product.py').write_text('# changed production input\n')
                path.write_text(json.dumps(record))
                (root/'logs/run.finalized').write_text('1\n' if defect=='fence' else '0\n')
                (root/'logs/raw.log').write_text('missing markers\n' if defect=='raw-log' else '\n'.join(case['success_marker'] for case in cases)+'\nELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS\n')
                matrix.write_text(yaml.safe_dump({'cases':current}))
                output=StringIO()
                with redirect_stdout(output):
                    if defect=='none':EVIDENCE.require_current(root,'contract')
                    else:
                        with self.assertRaises(ValueError):EVIDENCE.require_current(root,'contract')
                self.assertEqual('ELASTICSEARCH_CURRENT_EVIDENCE_REUSED scope=contract cases=4\n' if defect=='none' else '',output.getvalue())

    def test_actual_launch_reuses_current_contract_or_stops_at_failed_local_prerequisite(self):
        runner=(ROOT/'tests/helpers/elasticsearch-runner.sh').read_text()
        preflight=runner.split('elasticsearch_launch() {',1)[1].split('  run_id=',1)[0]
        for current,local_rc,expected in ((0,0,['refresh','require-current']),
                                         (1,0,['refresh','require-current','local-contract']),
                                         (1,7,['refresh','require-current','local-contract'])):
            with self.subTest(current=current,local_rc=local_rc),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);binary=root/'.venv/bin/python';binary.parent.mkdir(parents=True)
                binary.write_text('#!/bin/bash\nprintf "%s\\n" "$2" >> "$ROOT/calls"\nif [[ "$2" == require-current ]]; then exit '+str(current)+'; fi\n')
                binary.chmod(0o700)
                script='set -eu\nexport ROOT="$1"\nelasticsearch_local_gate() { echo local-contract >> "$ROOT/calls"; return '+str(local_rc)+'; }\nelasticsearch_launch() {'+preflight+'\n echo permitted >> "$ROOT/calls"; }\nelasticsearch_launch cleanup expected-marker\n'
                result=subprocess.run(['bash','-c',script,'contract',str(root)],capture_output=True,text=True,timeout=5)
                self.assertEqual(local_rc,result.returncode,result.stdout+result.stderr)
                self.assertEqual(expected+(['permitted'] if local_rc==0 else []),(root/'calls').read_text().splitlines())

    def test_recovery_identity_inventory_does_not_select_or_rewrite_configuration(self):
        config={'elasticsearch_namespace':'es-standard','elasticsearch_name':'search'}
        def get(kind,name='',namespace=''):
            if kind=='elasticsearch':return {'metadata':{'uid':'original-cluster'}}
            if kind=='pvc':return {'items':[{'metadata':{'name':'data','uid':'original-claim'}}]}
            if kind=='pods':return {'items':[]}
            if kind=='secret':return {'metadata':{'uid':'original-credential'},'data':{'elastic':'private-value'}}
            raise AssertionError(kind)
        with patch.object(LAB,'api',return_value=SimpleNamespace(get=get)),patch.object(LAB,'selected_config',side_effect=AssertionError('read-only inventory rewrote config')):
            actual=LAB.topology_state('standard',config=config)
        self.assertEqual('original-cluster',actual['cr_uid'])
        self.assertEqual({'data':'original-claim'},actual['pvc_uids'])
        self.assertEqual('original-credential',actual['credentials_uid'])
        self.assertNotIn('private-value',str(actual))

    def test_freeze_preserves_exact_source_bytes_separately_from_scoped_fingerprints(self):
        import tarfile
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source.py'
            original='def owned():\n    return 1\ndef unrelated():\n    return 2\n'
            source.write_text(original)
            inputs={'source.py#owned':MATRIX.dependency_digest(root,'source.py#owned')}
            directory=root/'logs/run'
            with patch.object(EVIDENCE,'launch_dependencies',return_value=inputs):
                EVIDENCE.freeze(root,directory,'business')
            with tarfile.open(directory/'launch-sources.tar.gz') as stream:
                self.assertEqual(original.encode(),stream.extractfile('source.py').read())
            self.assertEqual({'source.py':EVIDENCE.digest(source)},json.loads((directory/'launch-provenance.json').read_text()))
            source.write_text(original.replace('return 2','return 3'))
            self.assertEqual(inputs['source.py#owned'],MATRIX.dependency_digest(root,'source.py#owned'))
            self.assertNotEqual(EVIDENCE.digest(source),json.loads((directory/'launch-provenance.json').read_text())['source.py'])
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir()))

    def test_python_boundaries_preserve_unrelated_modules_and_follow_shared_callees(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for filename, roots, unrelated in (
                ('elasticsearch-lab.py', ('faults','security_certificates'), 'certificate_rotation'),
                ('elasticsearch-business.py', ('unavailable_write','security'), 'certificate_refusal')):
                path=root/filename;original=(ROOT/'tests/helpers'/filename).read_text();path.write_text(original)
                names=[filename+'#'+entry for entry in roots]
                before=[MATRIX.dependency_digest(root,name) for name in names]
                start=original.index('def '+unrelated+'(');body=original.index('\n',start)+1
                path.write_text(original[:body]+'    raise ValueError("changed local assertion")\n'+original[body:])
                after=[MATRIX.dependency_digest(root,name) for name in names]
                self.assertEqual(before[0],after[0]);self.assertNotEqual(before[1],after[1])
                shared='execute' if filename=='elasticsearch-lab.py' else 'require'
                start=original.index('def '+shared+'(');body=original.index('\n',start)+1
                path.write_text(original[:body]+'    raise ValueError("changed shared prerequisite")\n'+original[body:])
                after=[MATRIX.dependency_digest(root,name) for name in names]
                self.assertTrue(all(left!=right for left,right in zip(before,after)))
                path.write_text(original+'\n# commentary only\n')
                self.assertEqual(before,[MATRIX.dependency_digest(root,name) for name in names])
                with self.assertRaisesRegex(ValueError,'missing'):MATRIX.dependency_digest(root,filename+'#missing_owner')
                with self.assertRaisesRegex(ValueError,'escapes'):MATRIX.dependency_digest(root,'../foreign.py#security')

    def test_live_scope_does_not_bind_unconsumed_assertions_or_unit_test_sources(self):
        faults=EVIDENCE.dependencies(ROOT,'faults');certificates=EVIDENCE.dependencies(ROOT,'security-certificates')
        artifacts=EVIDENCE.dependencies(ROOT,'artifacts')
        self.assertIn('tests/helpers/elasticsearch-lab.py#faults',faults)
        self.assertIn('tests/helpers/elasticsearch-business.py#business,connect,unavailable_write',faults)
        self.assertIn('tests/helpers/elasticsearch-lab.py#security_certificates',certificates)
        self.assertIn('tests/helpers/longhorn-node-failure.py',faults)
        self.assertTrue(all(not name.startswith('tests/unit/') for name in faults))
        self.assertNotIn('tests/helpers/elasticsearch-network.py',faults)
        self.assertNotIn('tests/helpers/elasticsearch-business.py',artifacts)
        self.assertNotIn('tests/helpers/elasticsearch-lab.py',artifacts)
        self.assertIn('tests/evidence/elasticsearch-images-20261103.json',artifacts)
        self.assertIn('tests/helpers/elasticsearch-coexistence.py#logging_connection',EVIDENCE.dependencies(ROOT,'upgrade'))

    def test_upgrade_reuse_requires_archived_dr_and_binds_the_consumed_transport(self):
        import shutil
        for generation in ('legacy','current','missing-dr'):
            with self.subTest(generation=generation),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);name='tests/helpers/elasticsearch-coexistence.py';path=root/name
                path.parent.mkdir(parents=True);shutil.copy(ROOT/name,path)
                dr='tests/helpers/elasticsearch-dr.py'
                (root/dr).write_text('def upgrade(source):\n    return source\n' if generation=='legacy' else (ROOT/dr).read_text())
                checksum=EVIDENCE.digest(path);directory=root/'logs/original'
                inputs={name:checksum}
                if generation!='missing-dr':inputs[dr]=EVIDENCE.digest(root/dr)
                with patch.object(EVIDENCE,'launch_dependencies',return_value=inputs):EVIDENCE.freeze(root,directory,'upgrade')
                record={'dependencies':inputs,'provenance':[EVIDENCE.binding(root,directory/'launch-sources.tar.gz')]}
                original=path.read_text()
                path.write_text(original.replace('def unload_efk(lab):','def unload_efk(lab):\n    raise ValueError("changed retirement assertion")'))
                for cid in ('ES-50','ES-51','ES-52'):
                    self.assertEqual(generation!='missing-dr',MATRIX.dependency_matches(root,record,{'id':cid},name,checksum))
                path.write_text(original.replace('def logging_connection(lab):','def logging_connection(lab):\n    raise ValueError("changed consumed transport")'))
                self.assertEqual(generation=='legacy',MATRIX.dependency_matches(root,record,{'id':'ES-50'},name,checksum))
                for cid in ('ES-53','ES-54','ES-01'):self.assertFalse(MATRIX.dependency_matches(root,record,{'id':cid},name,checksum))
                self.assertFalse(MATRIX.dependency_matches(root,{}, {'id':'ES-50'},name,checksum))

    def test_recorded_case_dependencies_invalidate_only_the_owning_case(self):
        import shutil
        import yaml
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'tests/helpers').mkdir(parents=True);(root/'tests/evidence').mkdir()
            shutil.copy(ROOT/'tests/helpers/validate_elasticsearch_matrix.py',root/'tests/helpers')
            path=root/'shared.py';path.write_text('def first():\n    return 1\ndef second():\n    return 2\n')
            first={'shared.py#first':MATRIX.dependency_digest(root,'shared.py#first')}
            second={'shared.py#second':MATRIX.dependency_digest(root,'shared.py#second')}
            receipt={'dependencies':{'shared.py':EVIDENCE.digest(path)},'case_dependencies':{'ES-25':first,'ES-26':second}}
            (root/'tests/evidence/original.json').write_text(json.dumps(receipt))
            matrix=root/'tests/elasticsearch-test-matrix.yaml'
            matrix.write_text(yaml.safe_dump({'meta':{'result':'PENDING'},'acceptance':{'accepted':2},'cases':[
                {'id':cid,'status':'pass','evidence':['tests/evidence/original.json']} for cid in ('ES-25','ES-26')]}))
            self.assertEqual([],EVIDENCE.refresh(root))
            path.write_text(path.read_text().replace('return 1','return 3'))
            self.assertEqual(['ES-25'],EVIDENCE.refresh(root))
            cases=yaml.safe_load(matrix.read_text())['cases']
            self.assertEqual('pass',cases[1]['status']);self.assertEqual(['tests/evidence/original.json'],cases[1]['evidence'])
            self.assertEqual(receipt,json.loads((root/'tests/evidence/original.json').read_text()))

    def test_focused_entry_crosses_all_allowlists_and_dispatches_only_its_owner(self):
        import os
        runner=(ROOT/'tests/helpers/elasticsearch-runner.sh').read_text()
        gate=(ROOT/'tests/helpers/elasticsearch-gate.sh').read_text()
        remote=next(line for line in runner.splitlines() if line.startswith('case "$module" in '))
        allow=next(line for line in gate.splitlines() if line.startswith('case "$MODULE" in '))
        owners={'coexistence-restores':'coexistence_restores','security-identity':'security_identity','security-certificates':'security_certificates',
                'security-network':'security_network','security-s3':'security_s3',
                'security-certificate-restore':'security_certificate_restore'}
        for mode, owner in owners.items():
            script='source "$1"\nelasticsearch_launch() { printf "%s\\n" "$*"; }\nmodule="$2"\nMODULE="$2"\n'+remote+'\n'+allow+'\nelasticsearch_dispatch "--elasticsearch-$2"'
            result=subprocess.run(['bash','-eu','-c',script,'contract',str(ROOT/'tests/helpers/elasticsearch-runner.sh'),mode],text=True,capture_output=True,timeout=5)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual(mode+' ELASTICSEARCH_MODULE_PASS module='+mode,result.stdout.strip())
            with tempfile.TemporaryDirectory() as temporary,patch.object(LAB,'STATE',Path(temporary)),patch.object(LAB,owner) as invoke,patch.object(LAB,'security') as aggregate,patch.object(sys,'argv',['lab',mode]),patch.dict(os.environ,{'ELASTICSEARCH_PARENT_LEASE_HELD':'yes'}):
                LAB.main();invoke.assert_called_once_with();aggregate.assert_not_called()
        with patch.object(LAB,'security_identity',side_effect=ValueError('first owning module failed')),patch.object(LAB,'security_certificates') as certificates,patch.object(LAB,'security_network') as network,patch.object(LAB,'security_s3') as s3:
            with self.assertRaisesRegex(ValueError,'first owning'):LAB.security()
            certificates.assert_not_called();network.assert_not_called();s3.assert_not_called()

    def exercise_certificate_restore(self, defect=''):
        from contextlib import contextmanager
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary);events=[];live={};configs={};fixtures={};mutated=[False]
            for topology in ('standard','compact'):
                ns='es-'+topology
                config={'elasticsearch_namespace':ns,'elasticsearch_name':'search','elasticsearch_http_tls_secret':'','elasticsearch_requests':[]}
                configs[topology]=config;(state/('config-'+topology+'.json')).write_text(json.dumps(config))
                (state/('ledger-'+topology+'.json')).write_text(json.dumps({'index':'confirmed','documents':{'1':{'value':17}},'index_uuid':'index-'+topology}))
                live[ns]={'metadata':{'uid':'cr-'+topology},'spec':{'http':{'tls':{'certificate':{'secretName':'es-gate-http-b' if topology=='standard' else ''}}}},'status':{'health':'green'}}
            (state/'namespaces.json').write_text(json.dumps({'es-standard':'ns-standard','es-compact':'ns-compact'}))
            fixtures['es-gate-http-b']={'metadata':{'uid':'fixture-b','labels':dict(LAB.LABELS)},'type':'kubernetes.io/tls'}
            if defect=='foreign':fixtures['es-gate-http-b']['metadata']['labels']={}
            if defect=='fault':(state/'fault-unrestored.json').write_text('{}')
            if defect=='reference':live['es-standard']['spec']['http']['tls']['certificate']['secretName']='customer-unknown'
            def get(kind,name='',namespace=''):
                if kind=='namespace':return {'metadata':{'uid':'replaced' if defect=='namespace' and name=='es-compact' else 'ns-'+name.removeprefix('es-')}}
                if kind=='secret':
                    obj=fixtures.get(name) if namespace=='es-standard' else None
                    if obj and mutated[0] and defect=='fixture-uid':return dict(obj,metadata=dict(obj['metadata'],uid='replacement'))
                    return obj
                if kind=='elasticsearch':return live[namespace]
                raise AssertionError((kind,name,namespace))
            def run(*args,**kwargs):
                events.append('delete');self.assertTrue(mutated[0]);self.assertEqual('',live['es-standard']['spec']['http']['tls']['certificate'].get('secretName',''))
                fixtures.pop(args[4]);return ''
            api=SimpleNamespace(get=get,run=run)
            def product(*args,**kwargs):
                events.append('product');mutated[0]=True
                self.assertEqual(('setup',LAB.NAME,'07','--tags','elasticsearch'),args)
                if defect=='product':raise ValueError('product did not converge')
                live['es-standard']['spec']['http']['tls']['certificate']={}
            def identity(topology, **kwargs):
                return {'cr_uid':'changed' if mutated[0] and defect=='identity' else 'cr-'+topology,'pvc_uids':{'data':'pvc'},'credentials_uid':'credential','credentials_sha256':'hash','pod_uids':{'member':'new' if mutated[0] else 'old'}}
            @contextmanager
            def connection(config,k):
                topology=config['elasticsearch_namespace'].removeprefix('es-')
                self.assertEqual('es-gate-http-b' if topology=='standard' else '',config['elasticsearch_http_tls_secret'])
                def request(method,path,body=None):
                    events.append('read-'+topology)
                    if path.endswith('/_mget'):return {'docs':[] if defect=='data' else [{'_id':'1','found':True,'_source':{'value':17}}]}
                    if path.endswith('/_count'):return {'count':1}
                    if path.endswith('/_settings'):return {'confirmed':{'settings':{'index':{'uuid':'index-'+topology}}}}
                    raise AssertionError(path)
                yield request
            output=StringIO()
            from contextlib import redirect_stdout
            with patch.object(LAB,'STATE',state),patch.object(LAB,'baseline'),patch.object(LAB,'api',return_value=api),patch.object(LAB,'load_es',return_value=SimpleNamespace(connection=connection)),patch.object(LAB,'topology_state',side_effect=identity),patch.object(LAB,'selected_config',side_effect=lambda topology: configs[topology]),patch.object(LAB,'product',side_effect=product),patch.object(LAB,'verify_topology'),patch.object(LAB,'data_ledger'),redirect_stdout(output):
                if defect:
                    with self.assertRaises(ValueError):LAB.security_certificate_restore()
                    self.assertNotIn('delete',events)
                    if defect in ('foreign','namespace','fault','reference','data'):self.assertNotIn('product',events)
                    self.assertNotIn('SCOPED_CLEAN_PASS',output.getvalue())
                else:
                    LAB.security_certificate_restore()
                    self.assertEqual(1,events.count('product'));self.assertEqual(1,events.count('delete'))
                    self.assertLess(events.index('read-compact'),events.index('product'))
                    self.assertEqual({},fixtures)
                    self.assertIn('ELASTICSEARCH_CERTIFICATE_RESTORE_SCOPED_CLEAN_PASS',output.getvalue())
                    self.assertNotIn('ELASTICSEARCH_CASE_PASS',output.getvalue())

    def test_certificate_restore_reuses_both_baselines_and_allows_legitimate_pod_rolls(self):
        self.exercise_certificate_restore()

    def test_certificate_restore_refuses_unproven_boundaries_without_deleting_live_certificates(self):
        for defect in ('foreign','namespace','fault','reference','data','product','identity','fixture-uid'):
            with self.subTest(defect=defect):self.exercise_certificate_restore(defect)

if __name__ == '__main__':
    unittest.main()
