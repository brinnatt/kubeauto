"""Native UUID, shared-consumer and recovery contracts for control faults."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('control_faults', ROOT/'tests/helpers/longhorn-control-faults.py')
control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)
spec = importlib.util.spec_from_file_location('faults', ROOT/'tests/helpers/longhorn-faults.py')
faults = importlib.util.module_from_spec(spec); spec.loader.exec_module(faults)


class ControlFaultContracts(unittest.TestCase):
    def fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        catalog = base/'roles/longhorn/files/images.json'
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({'1.12.1': {'longhorn-instance-manager': 'v1.12.1'}}))
        def meta(name, uid, kind=None, owner=None):
            return {'name': name, 'uid': uid, 'namespace': 'longhorn-system', 'resourceVersion': '1',
                    'ownerReferences': [{'kind': kind, 'uid': owner, 'controller': True}] if kind else []}
        volume = {'metadata': meta('volume', 'volume-uid'),
                  'spec': {'dataEngine': 'v1', 'encrypted': False, 'accessMode': 'rwo'},
                  'status': {'state': 'attached', 'currentNodeID': 'longhorn-node-212'}}
        engine = {'metadata': meta('engine', 'engine-uid', 'Volume', 'volume-uid'),
                  'spec': {'volumeName': 'volume', 'nodeID': 'longhorn-node-212'},
                  'status': {'currentState': 'running', 'instanceManagerName': 'im212'}}
        managers = {}; pods = {}; replicas = []
        for n in (212, 165, 238):
            node = 'longhorn-node-' + str(n); name = 'im' + str(n)
            managers[name] = {'metadata': meta(name, name+'-uid'),
                              'spec': {'nodeID': node, 'dataEngine': 'v1'}, 'status': {'currentState': 'running'}}
            pods[name] = {'metadata': meta(name, name+'-pod', 'InstanceManager', name+'-uid'),
                          'spec': {'nodeName': node, 'containers': [{'name': 'instance-manager',
                                   'image': 'hub.talkedu.cn/kubeauto/longhorn-instance-manager:v1.12.1'}],
                                   'volumes': [{'secret': {'secretName': 'longhorn-grpc-tls'}}]},
                          'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
            replicas.append({'metadata': meta('replica'+str(n), 'r'+str(n), 'Volume', 'volume-uid'),
                             'spec': {'volumeName': 'volume', 'nodeID': node},
                             'status': {'instanceManagerName': name}})
        ds = {'metadata': meta('longhorn-manager', 'daemonset-uid')}
        ds['metadata']['labels'] = {'app.kubernetes.io/instance': 'longhorn'}
        pods['manager'] = {'metadata': meta('manager', 'manager-pod', 'DaemonSet', 'daemonset-uid'),
                           'spec': {'nodeName': 'longhorn-node-212'},
                           'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        pods['manager']['metadata']['labels'] = {'app': 'longhorn-manager'}
        process = {'spec': {'name': 'engine', 'binary': '/engine-binaries/longhorn', 'args': ['--engine-instance-name', 'engine', 'controller', 'volume']},
                   'status': {'state': 'running', 'uuid': '4c432de2-1b53-4678-8b43-6677c8883a12'}}
        namespace = {'metadata': meta('longhorn-system', 'namespace-uid')}
        volumes = [volume]
        def get(kind, name='', namespace=''):
            if kind == 'namespace': return copy.deepcopy(namespace_obj)
            if kind == 'volumes.longhorn.io': return copy.deepcopy(volume if name else {'items': volumes})
            if kind == 'engines.longhorn.io': return copy.deepcopy({'items': [engine]})
            if kind == 'replicas.longhorn.io': return copy.deepcopy({'items': replicas})
            if kind == 'instancemanagers.longhorn.io': return copy.deepcopy(managers.get(name))
            if kind == 'daemonset': return copy.deepcopy(ds)
            if kind == 'pod': return copy.deepcopy(pods.get(name))
            if kind == 'pods': return copy.deepcopy({'items': list(pods.values())})
            raise AssertionError(kind)
        namespace_obj = namespace
        def kubectl(*args, **kwargs):
            if 'list' in args:
                result = {r['metadata']['name']: {'spec': {'name': r['metadata']['name']}, 'status': {'state': 'running'}}
                          for r in replicas if r['status']['instanceManagerName'] == args[3]}
            else:
                result = copy.deepcopy(process)
                if 'delete' in args: result['deleted'] = True
            return SimpleNamespace(stdout=json.dumps(result))
        lab = SimpleNamespace(BASE=base, WORKERS=('192.168.122.212', '192.168.122.165', '192.168.122.238'),
            get=Mock(side_effect=get), kubectl=Mock(side_effect=kubectl), remove=Mock(),
            owner=Mock(return_value={'namespace_uid': 'namespace-uid'}), replica_topology=Mock(return_value={'r': 'healthy'}))
        journal = {'volume': 'volume', 'volume_uid': 'volume-uid', 'namespace': 'business'}
        return lab, journal, SimpleNamespace(volume=volume, volumes=volumes, engine=engine, managers=managers,
            pods=pods, replicas=replicas, process=process, namespace=namespace, ds=ds)

    def test_engine_and_remote_im_require_the_single_current_catalog_pin(self):
        lab, journal, objects = self.fixture()
        (lab.BASE/'roles/longhorn/files/images.json').write_text(json.dumps(
            {'1.12.1': {'longhorn-instance-manager': 'v1.12.1-kubeauto.1'}}))
        for name in ('im212', 'im165', 'im238'):
            objects.pods[name]['spec']['containers'][0]['image'] += '-kubeauto.1'
        engine = control.source(lab, journal)
        control.fault_pod(lab, journal, 'instance-manager', engine)
        objects.pods['im212']['spec']['containers'][0]['image'] = 'registry/longhorn-instance-manager:v1.12.1'
        with self.assertRaisesRegex(ValueError, 'identity'): control.source(lab, journal)
        objects.pods['im165']['spec']['containers'][0]['image'] = 'registry/longhorn-instance-manager:v1.12.1'
        with self.assertRaisesRegex(ValueError, 'pinned'): control.fault_pod(lab, journal, 'instance-manager', engine)

    def test_engine_deletion_uses_exact_native_uuid_and_refuses_changed_identity_or_response(self):
        for defect in ('none', 'namespace', 'volume', 'engine-owner', 'manager-owner', 'tls', 'unready',
                       'binary', 'args', 'engine-arg', 'legacy-args', 'uuid', 'response', 'race'):
            with self.subTest(defect=defect):
                lab, journal, objects = self.fixture()
                if defect == 'namespace': objects.namespace['metadata']['uid'] = 'foreign'
                if defect == 'volume': objects.volume['metadata']['uid'] = 'foreign'
                if defect == 'engine-owner': objects.engine['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if defect == 'manager-owner': objects.pods['im212']['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if defect == 'tls': objects.pods['im212']['spec']['volumes'] = []
                if defect == 'unready': objects.pods['im212']['status']['conditions'] = []
                if defect == 'binary': objects.process['spec']['binary'] = '/foreign'
                if defect == 'args': objects.process['spec']['args'][-1] = 'foreign'
                if defect == 'engine-arg': objects.process['spec']['args'][1] = 'foreign'
                if defect == 'legacy-args': objects.process['spec']['args'] = ['controller', 'volume']
                if defect == 'uuid': objects.process['status']['uuid'] = 'unknown'
                original = lab.kubectl.side_effect
                count = [0]
                def kubectl(*args, **kwargs):
                    result = original(*args, **kwargs); count[0] += 1
                    value = json.loads(result.stdout)
                    if defect == 'response' and 'delete' in args: value['status']['uuid'] = 'foreign'
                    if defect == 'race' and count[0] == 2: value['status']['uuid'] = 'ee78156b-942e-4c46-8749-45d27fef1986'
                    return SimpleNamespace(stdout=json.dumps(value))
                lab.kubectl.side_effect = kubectl
                with redirect_stdout(io.StringIO()):
                    if defect == 'none':
                        proof = control.inject(lab, journal, 'engine')
                        args = lab.kubectl.call_args.args
                        self.assertEqual(args[-5:], ('delete', '--name', 'engine', '--uuid', proof['engine']['uuid']))
                    else:
                        with self.assertRaises(ValueError): control.inject(lab, journal, 'engine')
                if defect not in ('none', 'response'):
                    self.assertFalse(any('delete' in call.args for call in lab.kubectl.call_args_list))
                lab.remove.assert_not_called()

    def test_shared_component_faults_refuse_foreign_consumers_and_changed_pod_parent_or_process(self):
        for variant in ('manager', 'instance-manager'):
            for defect in ('none', 'foreign-volume', 'parent', 'unready', 'pod-race', 'foreign-process'):
                if variant == 'manager' and defect == 'foreign-process': continue
                with self.subTest(variant=variant, defect=defect):
                    lab, journal, obj = self.fixture()
                    target = obj.pods['manager' if variant == 'manager' else 'im165']
                    if defect == 'foreign-volume': obj.volumes.append({'metadata': {'name': 'foreign', 'uid': 'f'}})
                    if defect == 'parent': target['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                    if defect == 'unready': target['status']['conditions'] = []
                    original = lab.get.side_effect
                    if defect == 'pod-race':
                        reads = [0]
                        def get(kind, *args, **kwargs):
                            value = original(kind, *args, **kwargs)
                            if kind == 'pod' and args[0] == target['metadata']['name']:
                                reads[0] += 1
                                if reads[0] >= (1 if variant == 'manager' else 2): value['metadata']['uid'] = 'replacement'
                            return value
                        lab.get.side_effect = get
                    if defect == 'foreign-process':
                        native = lab.kubectl.side_effect
                        def kubectl(*args, **kwargs):
                            result = native(*args, **kwargs)
                            if 'list' in args:
                                value = json.loads(result.stdout); value['foreign'] = {}
                                return SimpleNamespace(stdout=json.dumps(value))
                            return result
                        lab.kubectl.side_effect = kubectl
                    with redirect_stdout(io.StringIO()):
                        if defect == 'none': control.inject(lab, journal, variant)
                        else:
                            with self.assertRaises(ValueError): control.inject(lab, journal, variant)
                    self.assertEqual(lab.remove.call_count, int(defect == 'none'))
                    if defect == 'none': self.assertEqual(lab.remove.call_args.args[0]['metadata']['uid'], target['metadata']['uid'])

    def test_recovery_requires_new_native_uuid_or_ready_new_child_under_original_parent(self):
        for variant in ('engine', 'instance-manager', 'manager'):
            lab, journal, obj = self.fixture()
            with redirect_stdout(io.StringIO()): proof = control.inject(lab, journal, variant)
            self.assertFalse(control.recovered(lab, journal, proof))
            if variant == 'engine': obj.process['status']['uuid'] = 'ee78156b-942e-4c46-8749-45d27fef1986'
            else: obj.pods[proof['pod']]['metadata']['uid'] += '-new'
            self.assertTrue(control.recovered(lab, journal, proof))
            lab.replica_topology.return_value = None
            self.assertFalse(control.recovered(lab, journal, proof))
            lab.replica_topology.return_value = {'r': 'healthy'}
            if variant != 'engine':
                target = obj.ds if variant == 'manager' else obj.managers[proof['parent']['name']]
                target['metadata']['uid'] = 'foreign'
                with self.assertRaises(ValueError): control.recovered(lab, journal, proof)

    def test_only_complete_contiguous_acknowledgements_are_confirmed(self):
        token = 'contract'
        rows = [{'type': 'ack', 'sequence': n, 'sha256': hashlib.sha256(faults.ledger_block(token, n)).hexdigest()}
                for n in range(4)]
        proof = control.confirmed(rows + [{'type': 'retry', 'sequence': 4, 'errno': 'EIO'}], token, faults)
        self.assertEqual(proof['summary']['confirmed'], 4)
        for changed in (rows[:3], rows[::-1], rows + rows[:1], [dict(rows[0], sha256='foreign')] + rows[1:]):
            with self.assertRaises(ValueError): control.confirmed(changed, token, faults)
        with self.assertRaises(ValueError): control.confirmed(rows, 'different-volume', faults)

    def test_remount_checks_both_real_acknowledged_files_and_detects_corruption_in_either(self):
        for defect in ('none', 'original', 'recovered'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                proofs = []
                for name, token in (('fault-ledger', 'original'), ('control-recovered-ledger', 'recovered')):
                    rows = [{'type': 'ack', 'sequence': n, 'sha256': hashlib.sha256(faults.ledger_block(token, n)).hexdigest()}
                            for n in range(4)]
                    path = Path(directory)/name
                    path.write_bytes(b''.join(faults.ledger_block(token, n) for n in range(4)))
                    if defect == token:
                        with path.open('r+b') as stream: stream.seek(65536); stream.write(b'corrupt')
                    proofs.append(control.confirmed(rows, token, faults))
                proof = dict(proofs[0], fresh=dict(proofs[1], path='/data/control-recovered-ledger'))
                def kubectl(*args, **kwargs):
                    code = args[-1].replace('/data/', directory + '/')
                    return subprocess.run([sys.executable, '-c', code], input=kwargs['stdin'],
                                          text=True, capture_output=True, check=True, timeout=5)
                lab = SimpleNamespace(load_helper=Mock(return_value=faults), kubectl=Mock(side_effect=kubectl))
                with redirect_stdout(io.StringIO()) as output:
                    if defect == 'none':
                        control.verify_remount(lab, {'namespace': 'owned'}, 'reader', proof)
                        self.assertIn('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED', output.getvalue())
                    else:
                        with self.assertRaises(subprocess.CalledProcessError):
                            control.verify_remount(lab, {'namespace': 'owned'}, 'reader', proof)
                        self.assertNotIn('_VERIFIED', output.getvalue())


FAKE_KUBECTL = r'''
import builtins,json,os,signal,sys,time
from pathlib import Path
from types import SimpleNamespace
base=Path(__file__).resolve().parents[1]
data=base/'data';config=json.loads((base/'fixture.json').read_text())
def event(name,**fields):
 descriptor=os.open(base/'events.jsonl',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
 try:os.write(descriptor,(json.dumps(dict(event=name,pid=os.getpid(),**fields))+'\n').encode())
 finally:os.close(descriptor)
code=sys.argv[sys.argv.index('-c')+1]
if 'write_ledger("/data/fault-ledger",' in code:mode='writer'
elif 'write_ledger("/data/control-recovered-ledger",' in code:mode='fresh'
elif 'os.statvfs(' in code:mode='readonly-mount'
elif 'verify_ledger("/data/fault-ledger"' in code:mode='prefix-verify'
else:raise ValueError('unrecognized fixture kubectl request')
event(mode+'-start')
code=code.replace('/data/',str(data)+'/').replace('"/data"',repr(str(data)))
if mode=='writer':
 (base/'writer.pid').write_text(str(os.getpid()))
 term_count=0
 def terminate(*_):
  global term_count
  term_count+=1
  if config.get('hold_first_delete') and term_count==1:
   event('old-exec-still-running');return
  event('old-exec-terminated');sys.exit(143)
 signal.signal(signal.SIGTERM,terminate)
 real_pwrite=os.pwrite
 def write(descriptor,payload,offset):
  if (base/'read-only').exists():
   while config.get('delayed_readonly') and not (base/'callback-returned').exists():time.sleep(.01)
   raise OSError(30,'Read-only file system')
  return real_pwrite(descriptor,payload,offset)
 os.pwrite=write
elif mode=='readonly-mount':
 real_statvfs=os.statvfs
 def statvfs(path):
  row=real_statvfs(path)
  return SimpleNamespace(f_flag=row.f_flag|(os.ST_RDONLY if (base/'read-only').exists() and config['mount_flag_readonly'] else 0))
 os.statvfs=statvfs
real_print=builtins.print
def observed_print(*args,**kwargs):
 real_print(*args,**kwargs)
 if len(args)==1 and isinstance(args[0],str):
  try:row=json.loads(args[0])
  except (ValueError,TypeError):return
  if isinstance(row,dict) and row.get('type') in ('ack','retry','complete'):
   event(mode+'-'+row['type'],sequence=row.get('sequence'),errno=row.get('errno'))
builtins.print=observed_print
try:exec(compile(code,'<owning-ledger-program>','exec'))
except BaseException:
 event(mode+'-exit-error');raise
else:event(mode+'-completed')
'''


class ReadOnlyRecoveryProcessContracts(unittest.TestCase):
    """Real Popen/fsync/ledger verification; only API and mount-fault fixtures.

    No Kubernetes, mount or host mutation occurs. The executable kubectl maps
    the business mount into a temporary directory and injects EROFS only in
    the original writer process. All owning verifier/ledger code runs intact.
    """
    def fixture(self, base, *, readonly=True, foreign_at_fence=False,
                corrupt_prefix=False, hold_first_delete=False, native_timeout=False,
                delayed_readonly=False, mount_flag_readonly=True):
        (base/'kube-bin').mkdir();(base/'data').mkdir()
        (base/'fixture.json').write_text(json.dumps({'hold_first_delete':hold_first_delete,
                                                   'delayed_readonly':delayed_readonly,
                                                   'mount_flag_readonly':mount_flag_readonly}))
        executable=base/'kube-bin/kubectl'
        executable.write_text('#!'+sys.executable+'\n'+FAKE_KUBECTL);executable.chmod(0o700)
        journal={'namespace':'business','volume':'volume','volume_uid':'volume-uid',
                 'objects':{'Pod/writer':'original-writer-uid'}}
        state={'pod':{'metadata':{'name':'writer','uid':'original-writer-uid','resourceVersion':'7'},
            'spec':{'nodeName':'longhorn-node-146','volumes':[{'persistentVolumeClaim':{'claimName':'data'}}]},
            'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}},
            'native_recovered':False,'callback':False,'new_reads':0}
        def events():
            path=base/'events.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        def event(name,**fields):
            with (base/'events.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(event=name,**fields))+'\n')
        def get(kind,name='',namespace=''):
            if kind=='volumes.longhorn.io':
                return {'metadata':{'uid':'volume-uid'},'spec':{'dataEngine':'v2'},
                        'status':{'remountRequestedAt':'2026-10-21T10:35:15Z'}}
            if kind!='pod':raise AssertionError(kind)
            pod=state['pod']
            if name=='writer':
                if state['callback']:event('old-uid-fence',uid=(pod or {}).get('metadata',{}).get('uid'))
                return copy.deepcopy(pod) if pod and pod['metadata']['name']==name else None
            if name=='recovered-writer' and pod and pod['metadata']['name']==name:
                state['new_reads']+=1
                if state['new_reads']==1:return dict(copy.deepcopy(pod),status={'phase':'Pending','conditions':[]})
                event('new-pod-ready')
                if corrupt_prefix:
                    with (base/'data/fault-ledger').open('r+b') as stream:stream.write(b'corrupt')
                return copy.deepcopy(pod)
            return None
        def remove(pod):
            self.assertEqual(pod['metadata']['uid'],'original-writer-uid')
            event('delete-original-uid',uid=pod['metadata']['uid'])
            state['pod']=None
            os.kill(int((base/'writer.pid').read_text()),signal.SIGTERM)
        def wait(label,timeout,predicate):
            deadline=time.monotonic()+(0.3 if label=='control-fault-old-client-exited' and hold_first_delete else
                0.4 if label=='control-fault-native-recovered' and native_timeout else min(timeout,10))
            while True:
                value=predicate()
                if value:event('wait-complete',label=label);return value
                if time.monotonic()>=deadline:raise TimeoutError(label)
                time.sleep(0.01)
        def kubectl(*args,**kwargs):
            return subprocess.run([str(executable),*args],input=kwargs.get('stdin'),text=True,
                capture_output=True,check=True,timeout=kwargs.get('timeout',10))
        def create(pod):
            # A real Popen.poll must already have reaped the deleted exec.
            pid=int((base/'writer.pid').read_text())
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
            event('create-new-client')
            pod=copy.deepcopy(pod);pod['metadata']['uid']='replacement-writer-uid'
            pod['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
            state['pod']=pod
            (base/'read-only').unlink(missing_ok=True)
            return copy.deepcopy(pod)
        def inject(lab,journal,variant):
            event('native-fault-injected')
            (base/'read-only').touch()
            return {'variant':variant,'engine':{'node':'longhorn-node-146'},
                    'client_uid':'original-writer-uid','remount_requested_at':'',
                    'runtime':{'client_node':'longhorn-node-146'}}
        def recovered(lab,journal,proof):
            if native_timeout:return False
            if not delayed_readonly and not any(row['event']=='writer-retry' for row in events()):return False
            if not state['native_recovered']:
                event('native-recovered');state['native_recovered']=True
                if not readonly:(base/'read-only').unlink()
            return True
        runtime_spec=importlib.util.spec_from_file_location('process_readonly_runtime',ROOT/'tests/helpers/longhorn-v2-runtime-faults.py')
        runtime=importlib.util.module_from_spec(runtime_spec);runtime_spec.loader.exec_module(runtime)
        def callback(lab,journal,proof,rows):
            self.assertTrue(state['native_recovered'])
            event('callback');result=runtime.read_only_recovery(lab,journal,proof,rows)
            state['callback']=True
            (base/'callback-returned').touch()
            if result:event('callback-proof-confirmed')
            if foreign_at_fence:state['pod']['metadata']['uid']='foreign-writer-uid'
            return result
        lab=SimpleNamespace(BASE=base,DIRECTORY=base,load_helper=lambda _:faults,
            owner=lambda:{'run_id':'contract-run'},get=get,remove=remove,wait=wait,kubectl=kubectl,
            check_business_graph=lambda _:event('check-business-graph'),
            client_pod=lambda namespace,name,claim,node:{'metadata':{'name':name,'namespace':namespace},
                'spec':{'nodeName':node,'volumes':[{'persistentVolumeClaim':{'claimName':claim}}]}})
        return lab,journal,create,inject,recovered,callback,events

    def run_case(self,**kwargs):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        base=Path(directory.name)
        lab,journal,create,inject,recovered,callback,events=self.fixture(base,**kwargs)
        original_mkdtemp=tempfile.mkdtemp
        # Keep retained failure diagnostics inside the fixture's owned scope.
        patches=[patch.object(control,'inject',inject),patch.object(control,'recovered',recovered),
                 patch.object(control.tempfile,'mkdtemp',side_effect=lambda prefix,dir:original_mkdtemp(prefix=prefix,dir=base))]
        for item in patches:item.start();self.addCleanup(item.stop)
        output=io.StringIO()
        def run():
            with redirect_stdout(output):
                return control.verify(lab,journal,create,'instance-manager',
                    **({'read_only_recovery':callback} if kwargs.get('readonly',True) else {}))
        return base,run,events,output

    def test_real_process_readonly_recovery_fences_old_exec_then_verifies_original_and_fresh_data(self):
        base,run,events,output=self.run_case()
        proof=run();names=[row['event'] for row in events()]
        self.assertEqual(proof['client'],'recovered-writer')
        self.assertGreaterEqual(proof['summary']['confirmed'],4)
        self.assertGreaterEqual(proof['fresh']['summary']['confirmed'],4)
        expected=['native-recovered','callback-proof-confirmed','old-uid-fence','delete-original-uid',
                  'old-exec-terminated','create-new-client','new-pod-ready',
                  'prefix-verify-start','prefix-verify-completed','fresh-start','fresh-completed']
        positions=[names.index(name) for name in expected]
        self.assertEqual(positions,sorted(positions))
        rows=events()
        exit_wait=next(i for i,row in enumerate(rows) if row['event']=='wait-complete' and row['label']=='control-fault-old-client-exited')
        self.assertLess(exit_wait,names.index('create-new-client'))
        self.assertIn('documented-read-only-bare-pod-recreation',output.getvalue())
        self.assertEqual((base/'data/fault-ledger').read_bytes()[:4*faults.BLOCK],
                         b''.join(faults.ledger_block('contract-run:instance-manager',i) for i in range(4)))

    def test_original_writer_erofs_with_rw_mount_flag_still_fences_and_verifies_both_ledgers(self):
        _,run,events,output=self.run_case(mount_flag_readonly=False)
        proof=run();rows=events();names=[row['event'] for row in rows]
        self.assertEqual(proof['client'],'recovered-writer')
        self.assertGreaterEqual(proof['summary']['confirmed'],4)
        self.assertGreaterEqual(proof['fresh']['summary']['confirmed'],4)
        result=json.loads(next(line.split(' ',1)[1] for line in output.getvalue().splitlines()
            if line.startswith('LONGHORN_CONTROL_FAULT_RESULT ')))
        self.assertIs(result['read_only_recovery']['mount_read_only'],False)
        self.assertEqual(result['read_only_recovery']['last_retry']['errno'],'EROFS')
        expected=['native-recovered','callback-proof-confirmed','old-uid-fence','delete-original-uid',
                  'old-exec-terminated','create-new-client','new-pod-ready',
                  'prefix-verify-completed','fresh-completed']
        positions=[names.index(name) for name in expected]
        self.assertEqual(positions,sorted(positions))

    def test_callback_to_delete_uid_race_refuses_foreign_pod_without_delete_or_create(self):
        _,run,events,output=self.run_case(foreign_at_fence=True)
        with self.assertRaisesRegex(ValueError,'bare application Pod changed'):run()
        names=[row['event'] for row in events()]
        self.assertIn('callback',names);self.assertIn('old-uid-fence',names)
        self.assertNotIn('delete-original-uid',names);self.assertNotIn('create-new-client',names)
        self.assertNotIn('LONGHORN_CONTROL_FAULT_RESULT',output.getvalue())

    def test_old_exec_must_actually_exit_before_replacement_creation(self):
        _,run,events,_=self.run_case(hold_first_delete=True)
        with self.assertRaisesRegex(TimeoutError,'old-client-exited'):run()
        names=[row['event'] for row in events()]
        self.assertIn('old-exec-still-running',names)
        self.assertIn('old-exec-terminated',names)
        self.assertNotIn('create-new-client',names)

    def test_corrupt_original_ack_prefix_fails_before_fresh_write(self):
        _,run,events,output=self.run_case(corrupt_prefix=True)
        with self.assertRaises(subprocess.CalledProcessError):run()
        names=[row['event'] for row in events()]
        self.assertIn('prefix-verify-exit-error',names)
        self.assertNotIn('fresh-start',names)
        self.assertNotIn('LONGHORN_CONTROL_FAULT_RESULT',output.getvalue())

    def test_unrecovered_native_component_never_reaches_callback_or_pod_mutation(self):
        _,run,events,_=self.run_case(native_timeout=True)
        with self.assertRaisesRegex(TimeoutError,'native-recovered'):run()
        names=[row['event'] for row in events()]
        self.assertNotIn('callback',names);self.assertNotIn('delete-original-uid',names)
        self.assertNotIn('create-new-client',names)

    def test_default_v1_path_keeps_original_writer_and_requires_post_recovery_acknowledgements(self):
        _,run,events,output=self.run_case(readonly=False)
        result=run();names=[row['event'] for row in events()]
        self.assertEqual(result['client'],'writer')
        self.assertNotIn('callback',names);self.assertNotIn('delete-original-uid',names)
        self.assertNotIn('create-new-client',names)
        recovered=names.index('native-recovered')
        self.assertGreaterEqual(len([name for name in names[recovered:] if name=='writer-ack']),4)
        self.assertIn('writer-complete',names);self.assertIn('fresh-completed',names)
        self.assertIn('native-component-recreation-original-client',output.getvalue())

    def test_late_ero_fs_after_native_recovery_is_observed_within_existing_resume_wait(self):
        _,run,events,output=self.run_case(delayed_readonly=True)
        proof=run();names=[row['event'] for row in events()]
        self.assertEqual(proof['client'],'recovered-writer')
        self.assertLess(names.index('native-recovered'),names.index('callback'))
        self.assertLess(names.index('callback'),names.index('writer-retry'))
        self.assertLess(names.index('writer-retry'),names.index('callback-proof-confirmed'))
        self.assertGreaterEqual(names.count('callback'),2)
        self.assertEqual(names.count('delete-original-uid'),1)
        self.assertEqual(names.count('create-new-client'),1)
        self.assertLess(names.index('old-exec-terminated'),names.index('create-new-client'))
        self.assertLess(names.index('prefix-verify-completed'),names.index('fresh-start'))
        self.assertGreaterEqual(proof['fresh']['summary']['confirmed'],4)
        waits=[row['label'] for row in events() if row['event']=='wait-complete']
        self.assertEqual(waits.count('control-fault-post-recovery-writes'),1)
        self.assertIn('documented-read-only-bare-pod-recreation',output.getvalue())


if __name__ == '__main__':
    unittest.main()
