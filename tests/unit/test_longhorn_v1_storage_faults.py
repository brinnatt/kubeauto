"""V1 physical-disk and replica-network ownership and generated-code contracts."""
from contextlib import redirect_stdout
import ast
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('v1_storage_contract',ROOT/'tests/helpers/longhorn-v1-storage-faults.py')
M=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(M)


class NativeContracts(unittest.TestCase):
    def test_native_control_address_selects_only_actual_data_port(self):
        engine={'status':{'currentReplicaAddressMap':{'replica':'tcp://172.31.1.2:10000'}}}
        replica={'metadata':{'name':'replica'},'status':{'port':10000}}
        self.assertEqual(10001,M.data_endpoint(engine,replica,'172.31.1.2'))
        for address in ('tcp://192.168.47.131:10000','tcp://172.31.1.2:10001',
                        'udp://172.31.1.2:10000','tcp://user@172.31.1.2:10000','tcp://172.31.1.2:10000/path'):
            with self.subTest(address=address):
                engine['status']['currentReplicaAddressMap']['replica']=address
                with self.assertRaises(ValueError):M.data_endpoint(engine,replica,'172.31.1.2')

    def test_established_socket_is_exact_and_closed_or_control_ports_do_not_count(self):
        raw='sl local remote state\n0: 01011FAC:8000 02011FAC:2711 01\n'
        self.assertTrue(M.established_socket(raw,'172.31.1.1','172.31.1.2',10001))
        self.assertFalse(M.established_socket(raw,'172.31.1.1','172.31.1.2',10000))
        self.assertFalse(M.established_socket(raw.replace(' 01',' 0A'),'172.31.1.1','172.31.1.2',10001))

    def test_mountinfo_uses_kernel_identity_without_reading_offline_media(self):
        root='1 0 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n'
        raw=root+'2 1 8:16 / /data/longhorn rw,relatime - ext4 /dev/sdb rw\n'
        self.assertEqual({'writable':True},M.mount_identity(raw,'/data/longhorn','8:16'))
        self.assertEqual({'writable':False},M.mount_identity(raw.replace('rw,relatime - ext4 /dev/sdb','ro,relatime - ext4 /dev/sdb'),'/data/longhorn','8:16'))
        for changed in (raw.replace('8:16','8:1'),raw.replace('8:16','8:32'),raw.replace('ext4 /dev/sdb','xfs /dev/sdb'),root,raw+raw.splitlines()[1]+'\n'):
            with self.subTest(raw=changed),self.assertRaises(ValueError):M.mount_identity(changed,'/data/longhorn','8:16')

    def test_scsi_transition_never_adopts_a_changed_disk_or_replays_injection(self):
        original={'state':'running','writable':True,'uuid':'disk','machine':'machine'}
        self.assertEqual('offline',M.disk_transition(original,original,'offline'))
        offline=dict(original,state='offline',writable=False)
        self.assertEqual('running',M.disk_transition(offline,original,'restore'))
        for changed in (dict(offline,uuid='foreign'),dict(offline,machine='foreign'),dict(offline,state='blocked')):
            with self.subTest(changed=changed),self.assertRaises(ValueError):M.disk_transition(changed,original,'restore')
        with self.assertRaises(ValueError):M.disk_transition(offline,original,'offline')

    def test_generated_disk_code_writes_a_real_newline_and_does_not_probe_disk_uuid(self):
        source=M.disk_program();tree=ast.parse(source)
        writes=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='write_text']
        self.assertEqual(1,len(writes));self.assertEqual('\n',writes[0].args[0].right.value)
        self.assertNotIn('findmnt',source);self.assertIn("properties.get('ID_FS_UUID')",source)


class RuleContracts(unittest.TestCase):
    def execute(self,operation='add',host_network=False,existing=(),fail_insert=0):
        request={'operation':operation,'expected':{'machine_id':'machine','boot_id':'boot'},
            'source':'172.31.1.1','destination':'172.31.1.2','port':10001,'host_network':host_network,
            'comment':'kubeauto-longhorn-v1-'+'a'*24}
        chains=('OUTPUT','INPUT') if host_network else ('FORWARD','FORWARD')
        rules=[(chains[0],'-p','tcp','-s','172.31.1.1/32','-d','172.31.1.2/32','--dport','10001',
                '-m','comment','--comment',request['comment'],'-j','DROP'),
               (chains[1],'-p','tcp','-s','172.31.1.2/32','-d','172.31.1.1/32','--sport','10001',
                '-m','comment','--comment',request['comment'],'-j','DROP')]
        state={rules[index] for index in existing};calls=[];inserts=0
        def run(argv,**kwargs):
            nonlocal inserts
            calls.append(argv);action=argv[5]
            key=tuple([argv[6]]+argv[8:]) if action=='-I' else tuple(argv[6:])
            if action=='-C':return SimpleNamespace(returncode=0 if key in state else 1)
            if action=='-I':
                self.assertEqual('1',argv[7]);inserts+=1
                if inserts==fail_insert:raise subprocess.CalledProcessError(1,argv)
                state.add(key)
            elif action=='-D':state.remove(key)
            return SimpleNamespace(returncode=0)
        def read(path,*args,**kwargs):
            return {'/etc/machine-id':'machine','/proc/sys/kernel/random/boot_id':'boot'}[str(path)]
        output=io.StringIO();error=None
        with patch('sys.stdin',io.StringIO(json.dumps(request))),patch('pathlib.Path.read_text',read),patch('subprocess.run',side_effect=run),redirect_stdout(output):
            try:exec(compile(M.rule_program(),'<owned-v1-rules>','exec'),{})
            except Exception as caught:error=caught
        return request,rules,state,calls,output.getvalue(),error

    def test_actual_generated_rules_use_pod_forward_or_host_input_output(self):
        for host_network in (False,True):
            with self.subTest(host_network=host_network):
                request,rules,state,calls,output,error=self.execute(host_network=host_network)
                self.assertIsNone(error);self.assertEqual(set(rules),state)
                self.assertEqual([True,True],json.loads(output)['present'])
                self.assertTrue(all(request['comment'] in call for call in calls))
                self.assertEqual(['-C','-C'],[call[5] for call in calls[:2]])

    def test_collision_on_either_rule_prevents_all_mutation(self):
        for index in (0,1):
            with self.subTest(index=index):
                _,rules,state,calls,_,error=self.execute(existing=(index,))
                self.assertIsInstance(error,ValueError);self.assertEqual({rules[index]},state)
                self.assertFalse(any(call[5] in ('-I','-D') for call in calls))

    def test_partial_rule_write_can_be_removed_without_reinjecting(self):
        _,rules,state,_,_,error=self.execute(fail_insert=2)
        self.assertIsInstance(error,subprocess.CalledProcessError);self.assertEqual({rules[0]},state)
        _,_,state,calls,output,error=self.execute(operation='remove',existing=(0,))
        self.assertIsNone(error);self.assertFalse(state);self.assertEqual([False,False],json.loads(output)['present'])
        self.assertFalse(any(call[5]=='-I' for call in calls))


class SharedNetworkContracts(unittest.TestCase):
    def fixture(self):
        journal={'volume':'business','volume_uid':'business-uid'}
        allowed={'business':'business-uid','monitor':'monitor-uid'}
        volumes=[{'metadata':{'name':name,'uid':uid}} for name,uid in allowed.items()]
        lab=SimpleNamespace(get=Mock(return_value={'items':volumes}),check_business_graph=Mock())
        return lab,journal,allowed

    def test_shared_network_accepts_only_the_complete_exact_owned_volume_set(self):
        lab,journal,allowed=self.fixture()
        M.consumer_boundary(lab,journal,'network',allowed)
        lab.check_business_graph.assert_called_once_with(journal)
        for changed in ({'business':'business-uid'},dict(allowed,monitor='replacement'),
                        dict(allowed,foreign='foreign-uid'),{'monitor':'monitor-uid'},{}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                M.consumer_boundary(lab,journal,'network',changed)

    def test_shared_disk_fault_is_refused_before_any_native_or_host_action(self):
        lab,journal,allowed=self.fixture()
        with self.assertRaises(ValueError):M.consumer_boundary(lab,journal,'disk',allowed)
        lab.get.assert_not_called()

    def test_network_shared_boundary_uses_replica_chain_without_shared_process_fault(self):
        lab,journal,allowed=self.fixture()
        engine={'name':'engine','node':'node1','manager':'engine-im'}
        replicas=[{'metadata':{'name':'r'+str(i),'uid':'r-uid'+str(i)},
                   'spec':{'volumeName':'business','nodeID':'node'+str(i)}} for i in (2,3)]
        native={'manager':'replica-im','manager_uid':'im-uid','pod_uid':'pod-uid','node':'node2'}
        pod={'metadata':{'uid':'pod-uid'},'status':{'podIP':'172.31.1.2'}}
        def get(kind,name=None,namespace=None):
            if kind=='volumes.longhorn.io':return {'items':[{'metadata':{'name':n,'uid':u}} for n,u in allowed.items()]}
            if kind=='replicas.longhorn.io':return {'items':replicas}
            if kind=='pod':return pod if name=='replica-im' else {'metadata':{'uid':'source-uid'},'spec':{},'status':{'podIP':'172.31.1.1'}}
            if kind=='node':return {'metadata':{'uid':'node-uid'}}
            if kind=='engines.longhorn.io':return {}
            raise AssertionError((kind,name,namespace))
        lab.get=Mock(side_effect=get);lab.owner=lambda:{'run_id':'run','cluster_uid':'cluster'}
        lab.replica_topology=Mock(return_value={});lab.kubectl=Mock(return_value=SimpleNamespace(stdout='socket'))
        common=SimpleNamespace(source=Mock(side_effect=AssertionError('single-volume source used')),
            address=lambda lab,node:'192.168.122.212',host=lambda lab,ip:{'machine_id':'machine','boot_id':'boot'})
        control=SimpleNamespace(source=Mock(return_value=engine),fault_pod=Mock(side_effect=AssertionError('shared process fault used')))
        faults=SimpleNamespace(replica_process=Mock(return_value=native))
        modules={'longhorn-v1-stability.py':common,'longhorn-control-faults.py':control,'longhorn-faults.py':faults}
        with patch.object(M,'load',side_effect=modules.__getitem__),patch.object(M,'data_endpoint',return_value=10001),\
                patch.object(M,'established_socket',return_value=True),patch.object(M,'rule_call',return_value={'present':[False,False]}):
            proof=M.boundary(lab,journal,'network',allowed)
        self.assertEqual('r2',proof['replica']);self.assertEqual('pod-uid',proof['destination_pod_uid'])
        self.assertEqual('InstanceManager',proof['parent']['kind'])
        common.source.assert_not_called();control.fault_pod.assert_not_called()
        self.assertEqual(2,faults.replica_process.call_count)

    def test_changed_second_boundary_prevents_injection_and_journal_mutation(self):
        lab,journal,_=self.fixture()
        with patch.object(M,'boundary',side_effect=[{'identity':'old'},{'identity':'new'}]),\
                patch.object(M,'persist') as persist,patch.object(M,'rule_call') as rule:
            with self.assertRaisesRegex(ValueError,'changed before injection'):M.inject(lab,journal,'network')
        persist.assert_not_called();rule.assert_not_called()


class LogContracts(unittest.TestCase):
    def fixture(self):
        faults=M.load('longhorn-faults.py');control=M.load('longhorn-control-faults.py')
        token='run:instance-manager'
        def ledger(value):
            import hashlib
            records=[{'type':'ack','sequence':i,'sha256':hashlib.sha256(faults.ledger_block(value,i)).hexdigest()} for i in range(4)]
            return control.confirmed(records,value,faults)
        acknowledged=ledger(token);acknowledged['client']='writer'
        acknowledged['fresh']=dict(ledger(token+':recovered'),path='/data/control-recovered-ledger')
        topology={name:{'node':name,'uid':name+'-uid'} for name in ('a','b','c')}
        before={'case':'network','volume_uid':'volume-uid','intent':True,
            'degraded':{'volume_robustness':'degraded','attachment_uid':'single-csi','replica_mode':'ERR'},
            'replica':'c','replicas_before':topology,'rules_absent_before':True,
            'injected_rules':{'present':[True,True]},'source':'172.31.1.1','destination':'172.31.1.2','port':10001}
        after=dict(before,fault_restored=True,restored_rules={'present':[False,False]},
            recovered={'replicas':dict({k:copy.deepcopy(v) for k,v in topology.items() if k!='c'},d={'node':'c','uid':'d-uid'})})
        result={'variant':'instance-manager','client_rc':0,'confirmed':acknowledged['summary'],
            'fresh_confirmed':acknowledged['fresh']['summary']}
        return [('LONGHORN_V1_STORAGE_FAULT_INJECTED',before),('LONGHORN_V1_STORAGE_FAULT_RECOVERED',after),
            ('LONGHORN_CONTROL_FAULT_RESULT',result),('LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS',acknowledged),
            ('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED',{'original':acknowledged['summary'],'recovered':acknowledged['fresh']['summary']})]

    def render(self,rows):
        return '\n'.join([name+' '+json.dumps(row) for name,row in rows]+[
            'LONGHORN_CONTROL_FAILURE_BUSINESS_PASS','LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
            'LONGHORN_V1_STORAGE_FAULT_RESTORED_BEFORE_CLEANUP','LONGHORN_V1_STORAGE_NETWORK_BUSINESS_PASS'])

    def test_native_degradation_both_ledgers_and_remount_are_required(self):
        with redirect_stdout(io.StringIO()):M.verify_log(self.render(self.fixture()),'network')
        for mutation in ('degraded','attachment','rule','survivor','ledger','fresh','remount','marker','failure'):
            rows=copy.deepcopy(self.fixture())
            if mutation=='degraded':rows[0][1]['degraded']['volume_robustness']='healthy'
            if mutation=='attachment':rows[0][1]['degraded']['attachment_uid']=''
            if mutation=='rule':rows[1][1]['restored_rules']['present']=[False,True]
            if mutation=='survivor':rows[1][1]['recovered']['replicas']['a']['uid']='replacement'
            if mutation=='ledger':rows[3][1]['records'][0]['sha256']='0'*64
            if mutation=='fresh':rows[3][1]['fresh']['records'][0]['sha256']='0'*64
            if mutation=='remount':rows[4][1]['original']={}
            log=self.render(rows)
            if mutation=='marker':log=log.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS','')
            if mutation=='failure':log+='\nLONGHORN_FIRST_FAILURE phase=test'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):M.verify_log(log,'network')


class CleanupContracts(unittest.TestCase):
    def test_cli_owned_setup_failure_without_injected_fault_can_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);record=root/M.RECORD
            row={M.INTENT:{'run_id':'run','case':'network'}};record.write_text(json.dumps(row))
            original=Mock()
            lab=SimpleNamespace(DIRECTORY=root,owner=lambda:{'run_id':'run'},require_lease=Mock(),cleanup_business=original)
            with patch('sys.argv',['driver','cleanup','--case','network','--relay-pid','7']),\
                    patch.object(M,'load',return_value=lab),redirect_stdout(io.StringIO()):
                M.main()
            original.assert_called_once_with(record,relay_pid=7)

    def test_foreign_or_untyped_journal_cannot_call_generic_business_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);record=root/M.RECORD
            lab=SimpleNamespace(DIRECTORY=root,owner=lambda:{'run_id':'run'});original=Mock()
            for row in ({},{M.INTENT:{'run_id':'foreign','case':'network'}},{M.INTENT:{'run_id':'run','case':'wrong'}}):
                record.write_text(json.dumps(row))
                with self.assertRaisesRegex(ValueError,'unrelated business'):M.cleanup(lab,original)
                original.assert_not_called()

    def test_owned_setup_failure_without_fault_can_recover_its_business(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);record=root/M.RECORD
            row={M.INTENT:{'run_id':'run','case':'network'}};record.write_text(json.dumps(row))
            lab=SimpleNamespace(DIRECTORY=root,owner=lambda:{'run_id':'run'});original=Mock()
            M.cleanup(lab,original,relay_pid=7)
            original.assert_called_once_with(record,relay_pid=7)


class DiskRecoveryContracts(unittest.TestCase):
    def fixture(self):
        from tests.unit.test_longhorn_control_faults import ControlFaultContracts
        fixture = ControlFaultContracts(); self.addCleanup(fixture.doCleanups)
        lab, journal, obj = fixture.fixture()
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        lab.DIRECTORY = Path(directory.name)
        lab.NODES = ('192.168.122.135', '192.168.122.40', '192.168.122.72') + lab.WORKERS
        nodes = [{'metadata': {'name': 'longhorn-node-'+ip.rsplit('.', 1)[1], 'uid': 'node-'+ip},
                  'status': {'addresses': [{'type': 'InternalIP', 'address': ip}],
                             'conditions': [{'type': 'Ready', 'status': 'True'}]}} for ip in lab.NODES]
        state = {'run_id': 'run', 'cluster_uid': 'cluster', 'namespace_uid': 'namespace-uid',
                 'node_uids': {n['metadata']['name']: n['metadata']['uid'] for n in nodes},
                 'disks': {'192.168.122.238': {'device': '/dev/disk/by-id/original', 'uuid': 'filesystem'}}}
        lab.owner = lambda: state
        journal['pv'] = 'original-pv'
        original = lab.get.side_effect
        attachment = {'metadata': {'uid': 'attachment'}, 'spec': {'attacher': 'driver.longhorn.io',
                      'nodeName': 'longhorn-node-212', 'source': {'persistentVolumeName': journal['pv']}},
                      'status': {'attached': True}}
        def get(kind, name='', namespace=''):
            if kind == 'nodes': return {'items': nodes}
            if kind == 'namespace' and name == 'kube-system': return {'metadata': {'uid': state['cluster_uid']}}
            if kind == 'volumeattachments.storage.k8s.io': return {'items': [attachment]}
            if kind == 'replicas.longhorn.io' and name:
                return next((r for r in obj.replicas if r['metadata']['name'] == name), None)
            if kind == 'engines.longhorn.io' and name: return obj.engine
            return original(kind, name, namespace)
        lab.get.side_effect = get
        engine = M.load('longhorn-control-faults.py').source(lab, journal)
        topology = {r['metadata']['name']: {'uid': r['metadata']['uid'], 'node': r['spec']['nodeID']} for r in obj.replicas}
        for r in obj.replicas:
            r['status']['currentState'] = 'running'; r['spec']['healthyAt'] = 'before-fault'
        obj.engine['status']['replicaModeMap'] = {name: 'RW' for name in topology}
        obj.volume['status']['robustness'] = 'degraded'; obj.volume['spec']['numberOfReplicas'] = 3
        obj.replicas[-1]['spec']['failedAt'] = 'fault'; obj.replicas[-1]['status']['currentState'] = 'stopped'
        obj.engine['status']['replicaModeMap']['replica238'] = 'ERR'
        nodes[-1]['status']['conditions'][0]['status'] = 'Unknown'
        proof = {'case': 'disk', 'intent': True, 'run_id': 'run', 'cluster_uid': 'cluster',
                 'volume_uid': journal['volume_uid'], 'node': 'longhorn-node-238',
                 'node_uid': state['node_uids']['longhorn-node-238'], 'address': '192.168.122.238',
                 'disk': copy.deepcopy(state['disks']['192.168.122.238']), 'engine': engine,
                 'replica': 'replica238', 'replica_uid': 'r238', 'replicas_before': topology,
                 'attachment_uid': 'attachment'}
        journal[M.KEY] = proof; journal[M.INTENT] = {'run_id': 'run', 'case': 'disk'}
        record = lab.DIRECTORY/M.RECORD
        def save(): record.write_text(json.dumps(journal))
        lab.require_lease = Mock(); lab.core_identity = Mock(side_effect=ValueError('core node is not Ready'))
        lab.check_business_graph = Mock(); lab.replica_topology.return_value = None
        save()
        return lab, journal, proof, obj, nodes, state, attachment, save

    def test_original_degraded_disk_can_enter_cleanup_but_normal_source_stays_strict(self):
        lab, journal, proof, *_ = self.fixture()
        with self.assertRaisesRegex(ValueError, 'healthy owned'):
            M.load('longhorn-control-faults.py').source(lab, journal)
        M.disk_survivors(lab, journal, proof)
        with redirect_stdout(io.StringIO()): M.cleanup_preflight(lab, 'disk')
        lab.core_identity.assert_not_called(); lab.require_lease.assert_called_once()

    def test_recovery_refuses_other_failures_and_every_original_identity_drift(self):
        for defect in ('second-node', 'missing-node', 'node-ip', 'node-uid', 'cluster', 'namespace',
                       'run', 'intent', 'disk', 'victim', 'case', 'volume', 'foreign-volume',
                       'engine', 'native-uuid', 'manager', 'attachment', 'survivor', 'rw', 'topology'):
            with self.subTest(defect=defect):
                lab, journal, proof, obj, nodes, state, attachment, save = self.fixture()
                if defect == 'second-node': nodes[0]['status']['conditions'][0]['status'] = 'Unknown'
                if defect == 'missing-node': nodes.pop(0)
                if defect == 'node-ip': nodes[0]['status']['addresses'][0]['address'] = '192.168.47.129'
                if defect == 'node-uid': nodes[-1]['metadata']['uid'] = 'foreign'
                if defect == 'cluster': proof['cluster_uid'] = 'foreign'
                if defect == 'namespace': obj.namespace['metadata']['uid'] = 'foreign'
                if defect == 'run': journal[M.INTENT]['run_id'] = 'foreign'
                if defect == 'intent': proof['intent'] = False
                if defect == 'disk': proof['disk']['device'] = '/dev/disk/by-id/foreign'
                if defect == 'victim': proof['node'] = 'longhorn-node-165'
                if defect == 'case': proof['case'] = 'network'
                if defect == 'volume': obj.volume['metadata']['uid'] = 'foreign'
                if defect == 'foreign-volume': obj.volumes.append({'metadata': {'name': 'foreign', 'uid': 'foreign'}})
                if defect == 'engine': obj.engine['metadata']['uid'] = 'foreign'
                if defect == 'native-uuid': obj.process['status']['uuid'] = '36eafb1b-2dcb-4445-8079-8fcb22144e2e'
                if defect == 'manager': obj.managers['im212']['metadata']['uid'] = 'foreign'
                if defect == 'attachment': attachment['metadata']['uid'] = 'foreign'
                if defect == 'survivor': obj.replicas[0]['metadata']['uid'] = 'foreign'
                if defect == 'rw': obj.engine['status']['replicaModeMap']['replica165'] = 'ERR'
                if defect == 'topology': proof['replicas_before'].pop('replica165')
                save()
                with self.assertRaises(ValueError): M.cleanup_preflight(lab, 'disk')


class BootCheckContracts(unittest.TestCase):
    def fixture(self):
        uuid='1205083b-7a65-5ca6-ae8e-076c1202e68a';unit='systemd-fsck@dev-disk-by\\x2duuid-'+uuid+'.service'
        original={'machine_id':'machine','boot_id':'old-boot','identity':'hardware','filesystem_uuid':uuid,
            'receipt_sha256':'receipt','state':'running','writable':True,'device':'/dev/vdb','major_minor':[252,16],
            'attachment':{'kind':'virtio','path':'/sys/devices/pci/virtio7'}}
        disk={'path':'/data/kubeauto-longhorn','uuid':uuid,'device':'/dev/disk/by-path/extra'}
        proof={'original_host':original,'disk':disk,'run_id':'run'}
        row={'machine_id':'machine','boot_id':'old-boot','unit':unit,'fstab':[
            'UUID='+uuid+' /data/kubeauto-longhorn ext4 defaults 0 2 # kubeauto-longhorn-lab:run'],
            'guard':'[Unit]\nRequiresMountsFor=/data/kubeauto-longhorn\n[Service]\nExecStartPre=/usr/bin/mountpoint -q -- /data/kubeauto-longhorn\n',
            'override_files':[],'cmdline':['root=/dev/mapper/root','ro'],'checker':True,'label':'kca-longhorn',
            'locale':'LANG="en_US.UTF-8"\n','kernel':'5.14.0-687.10.1.el9_8.0.1.x86_64','mount_what':['/dev/disk/by-uuid/'+uuid],
            'versions':'systemd-252-67.el9.rocky.0.1.x86_64\ne2fsprogs-1.46.5-8.el9.x86_64\nutil-linux-2.37.4-25.el9.x86_64',
            'fsck_fragment':'[Service]\nExecStart=/usr/lib/systemd/systemd-fsck %f\n',
            'requires':[unit],'after':[unit],
            'fsck':{'FragmentPath':'/usr/lib/systemd/system/systemd-fsck@.service','DropInPaths':'',
                'ExecStart':'{ path=/usr/lib/systemd/systemd-fsck ; argv[]=/usr/lib/systemd/systemd-fsck /dev/disk/by-uuid/'+uuid+' ; ignore_errors=no ; }',
                'Result':'success','ExecMainStatus':'0','InvocationID':'a'*32,
                'ExecMainStartTimestampMonotonic':'100','ExecMainExitTimestampMonotonic':'200'},
            'mount':{'DropInPaths':'','Where':disk['path'],'ActiveEnterTimestampMonotonic':'300'}}
        proof['boot_preflight']=copy.deepcopy(row)
        row['boot_id']='12345678-1234-1234-1234-123456789abc'
        row['journal']=[{'MESSAGE':'kca-longhorn: clean, 11/100 files, 20/100 blocks',
            '_BOOT_ID':row['boot_id'].replace('-',''),'_SYSTEMD_INVOCATION_ID':'a'*32}]
        return proof,row

    def test_native_auto_preen_accepts_label_clean_and_full_summaries(self):
        for message in ('kca-longhorn: clean, 11/100 files, 20/100 blocks',
                        'kca-longhorn: 11/100 files (1.2% non-contiguous), 20/100 blocks'):
            proof,row=self.fixture();row['journal'][0]['MESSAGE']=message
            self.assertEqual(message,M.boot_check(row,proof)['terminal'])

    def test_successful_wrapper_cannot_hide_checker_errors_or_skip(self):
        for message in ('fsck failed with exit status 8.','Ignoring error.',
                        "fsck.ext4 doesn't exist, not checking file system.",
                        'skipping check','UNEXPECTED INCONSISTENCY; RUN fsck MANUALLY.'):
            proof,row=self.fixture();row['journal'].append({'MESSAGE':message})
            with self.subTest(message=message),self.assertRaises(ValueError):M.boot_check(row,proof)

    def test_current_boot_invocation_and_pre_mount_order_are_required(self):
        for mutation in ('boot','invocation','late','zero','exit','missing','path_prefix','duplicate'):
            proof,row=self.fixture()
            if mutation=='boot':row['journal'][0]['_BOOT_ID']='foreign'
            if mutation=='invocation':row['journal'][0]['_SYSTEMD_INVOCATION_ID']='b'*32
            if mutation=='late':row['fsck']['ExecMainExitTimestampMonotonic']='301'
            if mutation=='zero':row['fsck']['ExecMainStartTimestampMonotonic']='0'
            if mutation=='exit':row['fsck']['ExecMainStatus']='8'
            if mutation=='missing':row['journal']=[]
            if mutation=='path_prefix':row['journal'][0]['MESSAGE']='/dev/vdb: clean, 11/100 files, 20/100 blocks'
            if mutation=='duplicate':row['journal']*=2
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):M.boot_check(row,proof)

    def test_changed_boot_policy_or_unsafe_override_stops(self):
        for mutation in ('fstab','dropin','guard','skip','force','repair','fastboot','checker','requires','what','version'):
            proof,row=self.fixture()
            if mutation=='fstab':row['fstab'][0]=row['fstab'][0].replace(' 0 2 ',' 0 0 ')
            if mutation=='dropin':row['fsck']['DropInPaths']='/etc/systemd/system/custom.conf'
            if mutation=='guard':row['guard']=''
            if mutation in ('skip','force'):row['cmdline'].append('fsck.mode='+mutation)
            if mutation=='repair':row['cmdline'].append('fsck.repair=yes')
            if mutation=='fastboot':row['override_files']=['/fastboot']
            if mutation=='checker':row['checker']=False
            if mutation=='requires':row['requires']=[]
            if mutation=='what':row['mount_what']=['/dev/vdb']
            if mutation=='version':row['versions']=row['versions'].replace('systemd-252-','systemd-999-')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):M.boot_check(row,proof)

    def test_virtio_new_name_and_boot_never_relax_hardware_or_receipt(self):
        proof,_=self.fixture();before=proof['original_host'];after=dict(before,boot_id='new-boot',device='/dev/vde',major_minor=[252,64])
        M.restored_disk_identity(after,before,reboot=True)
        for key in ('identity','machine_id','filesystem_uuid','receipt_sha256','attachment'):
            changed=dict(after);changed[key]='foreign'
            with self.subTest(key=key),self.assertRaises(ValueError):M.restored_disk_identity(changed,before,reboot=True)
        for changed in (dict(after,boot_id='old-boot'),dict(after,writable=False),dict(after,state='offline')):
            with self.assertRaises(ValueError):M.restored_disk_identity(changed,before,reboot=True)
        scsi={k:v for k,v in before.items() if k!='attachment'}
        with self.assertRaises(ValueError):M.restored_disk_identity(dict(scsi,device='/dev/sdc'),scsi)

    def generated_boot_probe(self, fail_journal=None):
        proof,row=self.fixture();calls=[]
        mount='data-kubeauto\\x2dlonghorn.mount'
        row['mount'].update(FragmentPath='/run/systemd/generator/'+mount,Requires=row['unit'],After=row['unit'])
        reads={row['mount']['FragmentPath']:'[Mount]\nWhat='+row['mount_what'][0]+'\n',
            row['fsck']['FragmentPath']:row['fsck_fragment'],
            '/etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf':row['guard'],
            '/etc/fstab':'\n'.join(row['fstab']),'/proc/sys/kernel/random/boot_id':row['boot_id'],
            '/etc/machine-id':row['machine_id'],'/proc/cmdline':' '.join(row['cmdline']),
            '/etc/locale.conf':row['locale']}
        def run(argv,**kwargs):
            self.assertEqual({'check':True,'capture_output':True,'text':True,'timeout':30},kwargs)
            calls.append(argv)
            if argv[0]=='systemd-escape':value=row['unit'] if '--template=systemd-fsck@.service' in argv else mount
            elif argv[0]=='systemctl':value='\n'.join(key+'='+value for key,value in row['fsck' if argv[2]==row['unit'] else 'mount'].items())
            elif argv[0]=='blkid':value=row['label']
            elif argv[0]=='rpm':value=row['versions']
            elif argv[0]=='uname':value=row['kernel']
            elif argv[0]=='journalctl':
                scope='kernel' if '-k' in argv else 'fsck'
                if scope==fail_journal:raise subprocess.CalledProcessError(1,argv,output='',stderr='journal read denied')
                value=json.dumps({'MESSAGE':scope})
            else:raise AssertionError(argv)
            return SimpleNamespace(stdout=value)
        output=io.StringIO()
        with patch('sys.stdin',io.StringIO(json.dumps({'disk':proof['disk'],'journal':True,'warnings':True}))), \
                patch('pathlib.Path.read_text',lambda path,*a,**k:reads[str(path)]), \
                patch('pathlib.Path.exists',return_value=False),patch('pathlib.Path.is_file',return_value=True), \
                patch('subprocess.run',side_effect=run),redirect_stdout(output):
            exec(compile(M.boot_check_program(),'<owned-v1-boot-check>','exec'),{})
        return row,calls,json.loads(output.getvalue())

    def test_generated_journal_calls_bind_original_boot_as_32_hex_descriptor(self):
        row,calls,observed=self.generated_boot_probe()
        journal=[argv for argv in calls if argv[0]=='journalctl']
        boot='--boot='+row['boot_id'].replace('-','')
        self.assertEqual([
            ('journalctl','--quiet','--no-pager',boot,'-u',row['unit'],'-o','json'),
            ('journalctl','--quiet','--no-pager','-k',boot,'-p','warning','-n','200','-o','json')],journal)
        self.assertEqual(row['boot_id'],observed['boot_id'])
        self.assertEqual([{'MESSAGE':'fsck'}],observed['journal'])
        self.assertEqual([{'MESSAGE':'kernel'}],observed['kernel_warnings'])
        ast.parse(M.boot_check_program(),feature_version=(3,8))

    def test_generated_journal_errors_still_abort_both_observations(self):
        for scope in ('fsck','kernel'):
            with self.subTest(scope=scope),self.assertRaises(subprocess.CalledProcessError) as raised:
                self.generated_boot_probe(fail_journal=scope)
            self.assertEqual(1,raised.exception.returncode)
            self.assertEqual('journal read denied',raised.exception.stderr)

    def test_generated_probe_never_runs_checker_or_changes_boot_policy(self):
        source=M.boot_check_program();ast.parse(source);ast.parse(M.virtio_program())
        self.assertNotIn('write_text',source);self.assertNotIn("run('fsck'",source)
        self.assertNotIn('remount',source)


class DiskLogContracts(unittest.TestCase):
    def disk_log(self):
        rows=LogContracts().fixture();proof,observed=BootCheckContracts().fixture()
        before=rows[0][1];before.update(case='disk',original_host=proof['original_host'],
            disk=proof['disk'],run_id=proof['run_id'],boot_preflight=proof['boot_preflight'],disk_uuid='native-disk')
        attachment=before['original_host']['attachment']
        before['injected_driver']={'bound':False,'blocks':[],'attachment':attachment}
        after=copy.deepcopy(before);after.update(fault_restored=True,
            restored_host=dict(before['original_host'],boot_id=observed['boot_id'],device='/dev/vde',major_minor=[252,64]),
            restored_driver={'bound':True,'blocks':['vde'],'attachment':attachment},
            boot_recovery={'intent':True,'original':{k:before['original_host'][k] for k in ('machine_id','boot_id')},
                'observed':observed,'checker':M.boot_check(observed,proof)},recovered=rows[1][1]['recovered'])
        rows[1]=(rows[1][0],after)
        return rows

    def render(self,rows):
        return LogContracts().render(rows).replace('LONGHORN_V1_STORAGE_NETWORK_BUSINESS_PASS','LONGHORN_V1_STORAGE_DISK_BUSINESS_PASS')

    def test_disk_log_binds_actual_detach_and_new_boot_checker_to_original_input(self):
        with redirect_stdout(io.StringIO()):M.verify_log(self.render(self.disk_log()),'disk')
        for mutation in ('still_bound','no_reboot','receipt','intent','wrapper_error','checker_digest'):
            rows=self.disk_log();before,after=rows[0][1],rows[1][1]
            if mutation=='still_bound':before['injected_driver']['bound']=True
            if mutation=='no_reboot':after.pop('boot_recovery')
            if mutation=='receipt':after['restored_host']['receipt_sha256']='foreign'
            if mutation=='intent':after['boot_recovery']['intent']=False
            if mutation=='wrapper_error':after['boot_recovery']['observed']['journal'].append({'MESSAGE':'fsck failed with exit status 8.'})
            if mutation=='checker_digest':after['boot_recovery']['checker']['journal_sha256']='foreign'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):M.verify_log(self.render(rows),'disk')


class VirtioDriverContracts(unittest.TestCase):
    def execute(self,operation,bound=True,fail_after_write=False):
        attachment={'kind':'virtio','bus_id':'virtio7','path':'/sys/devices/pci0000:00/0000:08:00.0/virtio7',
            'pci':'/sys/devices/pci0000:00/0000:08:00.0','driver':'/sys/bus/virtio/drivers/virtio_blk'}
        import hashlib
        request={'operation':operation,'attachment':attachment,'size':1024,
            'disk':{'device':'/dev/disk/by-path/extra','identity':'path:pci-extra','uuid':'uuid'},
            'original':{'machine_id':'machine','boot_id':'boot','device':'/dev/vdb',
                'receipt_sha256':hashlib.sha256(b'receipt').hexdigest()}}
        state={'bound':bound,'name':'vdb','writes':[]}
        bus=Path(attachment['path'])
        def resolve(path,strict=False):
            if str(path)=='/dev/disk/by-path/extra':return Path('/dev')/state['name']
            if str(path)==str(bus/'subsystem'):return Path('/sys/bus/virtio')
            if str(path)==str(bus/'driver'):return Path(attachment['driver'])
            return path
        def read(path,*args,**kwargs):
            if str(path).endswith('/size'):return '2'
            return {'/etc/machine-id':'machine','/proc/sys/kernel/random/boot_id':'boot',str(bus/'device'):'0x0002'}[str(path)]
        def exists(path):return state['bound'] if path in (bus/'driver',bus/'block') else False
        def iterdir(path):return iter([bus/'block'/state['name']]) if path==bus/'block' else iter([])
        def glob(path,pattern):return iter([bus]) if path==bus.parent and pattern=='virtio*' else iter([])
        def write(path,value,*args,**kwargs):
            state['writes'].append((str(path),value));state['bound']=path.name=='bind'
            if state['bound']:state['name']='vde'
            if fail_after_write:raise OSError('lost completion')
        output=io.StringIO();error=None
        with patch('sys.stdin',io.StringIO(json.dumps(request))),patch('pathlib.Path.resolve',resolve),\
                patch('pathlib.Path.read_text',read),patch('pathlib.Path.exists',exists),\
                patch('pathlib.Path.iterdir',iterdir),patch('pathlib.Path.glob',glob),\
                patch('pathlib.Path.write_text',write),patch('pathlib.Path.read_bytes',return_value=b'receipt'),\
                patch('subprocess.run',return_value=SimpleNamespace(stdout='ID_PATH=pci-extra\nID_FS_UUID=uuid\n')),redirect_stdout(output):
            try:exec(compile(M.virtio_program(),'<virtio-driver>','exec'),{})
            except Exception as caught:error=caught
        return state,output.getvalue(),error

    def test_real_driver_request_uses_only_original_virtio_bus_id(self):
        for operation,bound in (('unbind',True),('bind',False)):
            state,out,error=self.execute(operation,bound)
            self.assertIsNone(error);self.assertEqual([('/sys/bus/virtio/drivers/virtio_blk/'+operation,'virtio7\n')],state['writes'])
            self.assertEqual(operation=='bind',json.loads(out)['bound'])
        state,out,error=self.execute('bind',True)
        self.assertIsNone(error);self.assertEqual([],state['writes'])
        self.assertEqual(['vdb'],json.loads(out)['blocks'])

    def test_repeated_unbind_stops_and_lost_response_does_not_hide_state(self):
        state,_,error=self.execute('unbind',False)
        self.assertIsInstance(error,ValueError);self.assertEqual([],state['writes'])
        state,_,error=self.execute('unbind',True,fail_after_write=True)
        self.assertIsInstance(error,OSError);self.assertFalse(state['bound'])

    def test_first_reboot_saves_intent_before_request_and_cleanup_never_replays(self):
        proof,row=BootCheckContracts().fixture();proof.update(address='worker',node='node',node_policy={})
        current=dict(proof['original_host'],boot_id=row['boot_id'],device='/dev/vde',major_minor=[252,64])
        saved=[];original={k:proof['original_host'][k] for k in ('machine_id','boot_id')}
        common=SimpleNamespace(host=Mock(side_effect=[original,{k:current[k] for k in original},
            {k:current[k] for k in original}]),node_restored=Mock(return_value=True))
        def reboot(*args,**kwargs):
            self.assertTrue(saved[-1]['intent']);self.assertEqual(original,saved[-1]['original'])
        runtime=SimpleNamespace(host_call=Mock(side_effect=reboot))
        relay=SimpleNamespace(reboot=lambda lab,pid,address,original:runtime.host_call(lab,address,{'action':'reboot','expected':original},check=False))
        lab=SimpleNamespace(wait=lambda name,timeout,callback:callback())
        def persist(*args):saved.append(copy.deepcopy(proof['boot_recovery']))
        with patch.object(M,'load',side_effect=lambda name:common if name=='longhorn-v1-stability.py' else relay),\
                patch.object(M,'disk_call',return_value=current),\
                patch.object(M,'boot_observe',side_effect=[proof['boot_preflight'],row,row]),\
                patch.object(M,'disk_survivors'),patch.object(M,'persist',side_effect=persist):
            journal={M.INTENT:{'relay_pid':123}}
            M.disk_reboot(lab,journal,proof);M.disk_reboot(lab,journal,proof)
        self.assertEqual(1,runtime.host_call.call_count)

    def test_reentry_waits_for_intended_boot_without_second_reboot(self):
        proof,row=BootCheckContracts().fixture();proof.update(address='worker',node='node',node_policy={})
        proof['boot_recovery']={'intent':True,'original':{k:proof['original_host'][k] for k in ('machine_id','boot_id')}}
        current=dict(proof['original_host'],boot_id=row['boot_id'],device='/dev/vde',major_minor=[252,64])
        common=SimpleNamespace(host=Mock(return_value={k:current[k] for k in ('machine_id','boot_id')}),node_restored=Mock(return_value=True))
        runtime=SimpleNamespace(host_call=Mock(side_effect=AssertionError('reboot replayed')))
        lab=SimpleNamespace(wait=lambda name,timeout,callback:callback())
        with patch.object(M,'load',side_effect=lambda name:common if name=='longhorn-v1-stability.py' else runtime),\
                patch.object(M,'disk_call',return_value=current),patch.object(M,'boot_observe',return_value=row),\
                patch.object(M,'disk_survivors'),patch.object(M,'persist'):
            M.disk_reboot(lab,{},proof)
        runtime.host_call.assert_not_called();self.assertEqual(current,proof['restored_host'])


if __name__=='__main__':unittest.main()
