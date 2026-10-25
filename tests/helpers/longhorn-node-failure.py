#!/usr/bin/env python3
"""LONGHORN-20: fenced attached-worker loss and Deployment rescheduling.

The guest remains reachable for positive isolation proof. This is a controlled
guest-runtime fence followed by a real reboot, not hypervisor power fencing or
an availability guarantee. SSH loss and NotReady never authorize force deletion.
"""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import re
import secrets
import signal
import sys

NS='longhorn-system'
RECORD='longhorn-node-failure.json'
BUSINESS='longhorn-node-failure-business.json'


def load(name):
    spec=importlib.util.spec_from_file_location('node20_'+name.replace('-','_'),Path(__file__).with_name(name))
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def guest_fence(request,action,root='/'):
    """Journal a reversible guest fence before any service/container mutation."""
    import json
    import os
    from pathlib import Path
    import re
    import stat
    import subprocess

    root=Path(root);token=request['token']
    if not re.fullmatch('[a-f0-9]{32}',token) or action not in ('inspect','fence','probe','restore'):
        raise ValueError('invalid worker fence owner or action')
    receipt=root/('run/kubeauto-longhorn-node20-'+token+'.json')
    units=('kubelet.service','containerd.service')
    body='[Unit]\nRefuseManualStart=yes\n[Service]\nRestart=no\n# kubeauto-node20 '+token+'\n'
    guards=[root/('run/systemd/system/'+unit+'.d/kubeauto-node20-'+token+'.conf') for unit in units]
    def command(args,check=True):
        result=subprocess.run(args,check=False,text=True,capture_output=True,timeout=60)
        if check and result.returncode:raise ValueError('worker fence command failed: '+args[0])
        return result
    def machine():
        return {'machine_id':(root/'etc/machine-id').read_text().strip(),
                'boot_id':(root/'proc/sys/kernel/random/boot_id').read_text().strip()}
    def states():
        output={}
        for unit in units:
            raw=command(['systemctl','show',unit,'--property=ActiveState,UnitFileState']).stdout
            output[unit]=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
        return output
    def tasks():
        output=[]
        for item in (root/'proc').iterdir():
            if not item.name.isdigit():continue
            try:
                if 'kubepods' in (item/'cgroup').read_text() and (item/'stat').read_text().rsplit(')',1)[1].split()[0]!='Z':
                    output.append(int(item.name))
            except (FileNotFoundError,ProcessLookupError):continue
        return sorted(output)
    def cri(*args):
        return command(['/usr/local/bin/crictl','--runtime-endpoint','unix:///run/containerd/containerd.sock',*args])
    def containers():return json.loads(cri('ps','-o','json').stdout).get('containers',[])
    def sandboxes():return json.loads(cri('pods','--state','ready','-o','json').stdout).get('items',[])
    def allowed(rows):
        for item in rows:
            uid=item.get('labels',{}).get('io.kubernetes.pod.uid') or item.get('metadata',{}).get('uid')
            if uid not in request['pod_uids']:raise ValueError('worker fence refuses an unowned running Pod')
    def write_exclusive(path,text):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
    def read_receipt():
        if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_uid!=os.geteuid():
            raise ValueError('worker fence lacks its regular owned receipt')
        row=json.loads(receipt.read_text())
        if row['request']!=request:raise ValueError('worker fence receipt identity changed')
        return row
    def check_guard(path):
        if (path.is_symlink() or not path.is_file() or path.read_text()!=body or
                path.stat().st_uid!=os.geteuid() or stat.S_IMODE(path.stat().st_mode)!=0o600):
            raise ValueError('worker fence guard replaced or unconfirmed')
    if machine()!=request['host']:raise ValueError('worker fence machine or boot identity changed')
    if action=='inspect':
        if receipt.exists() or receipt.is_symlink() or any(p.exists() or p.is_symlink() for p in guards):
            raise ValueError('worker fence refuses existing runtime material')
        state=states()
        if any(s.get('ActiveState')!='active' or s.get('UnitFileState') not in ('enabled','disabled','static','indirect') for s in state.values()):
            raise ValueError('worker fence requires original active unmasked services')
        allowed(containers());allowed(sandboxes())
        return {'host':machine(),'units':state}
    if action=='fence':
        original=guest_fence(request,'inspect',root)
        if original!=request['original']:raise ValueError('worker fence original service state changed')
        created_dirs=[str(path.parent) for path in guards if not path.parent.exists()]
        write_exclusive(receipt,json.dumps({'request':request,'created_dirs':created_dirs},sort_keys=True))
        for path in guards:
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.parent.is_symlink():raise ValueError('worker fence refuses a symlink guard directory')
            write_exclusive(path,body)
        command(['systemctl','daemon-reload'])
        command(['systemctl','stop','kubelet.service'])
        running=containers();pods=sandboxes();allowed(running);allowed(pods)
        for item in running:cri('stop','--timeout','10',item['id'])
        for item in pods:cri('stopp',item['id'])
        if containers() or sandboxes():raise ValueError('worker fence still has running CRI workloads')
        command(['systemctl','stop','containerd.service'])
    if action=='restore':
        if not receipt.exists() and not receipt.is_symlink():
            if states()!=request['original']['units'] or any(p.exists() or p.is_symlink() for p in guards):
                raise ValueError('worker fence has no receipt for changed services')
            return {'host':machine(),'units':states(),'restored':True}
        saved=read_receipt()
        for path in guards:
            if path.exists() or path.is_symlink():check_guard(path)
        for path in guards:
            if path.exists():path.unlink()
        for path in guards:
            if str(path.parent) in saved.get('created_dirs',[]):
                try:path.parent.rmdir()
                except OSError:pass
        command(['systemctl','daemon-reload'])
        command(['systemctl','start','containerd.service'])
        command(['systemctl','start','kubelet.service'])
        if states()!=request['original']['units']:raise ValueError('worker fence original service state not restored')
        receipt.unlink()
        return {'host':machine(),'units':states(),'restored':True}
    read_receipt()
    for path in guards:check_guard(path)
    state=states();active=tasks()
    if any(s.get('ActiveState')!='inactive' for s in state.values()) or active:
        raise ValueError('worker fence is not proven: live service or kubepods processes')
    return {'host':machine(),'units':state,'kubepods_pids':active,'fenced':True,'scope':'guest-runtime'}


class Failure:
    def __init__(self,lab,pid):
        self.lab,self.pid=lab,pid
        self.maintenance=load('longhorn-maintenance.py')
        self.maintenance.RECORD=RECORD;self.maintenance.BUSINESS=BUSINESS
        self.common=load('longhorn-v1-stability.py')
        self.path=lab.DIRECTORY/RECORD;self.row=None

    def save(self):self.lab.write_private(self.path,self.row)

    def read(self):
        if self.path.is_symlink():raise ValueError('symlink attached-worker recovery receipt')
        self.row=json.loads(self.path.read_text());state=self.lab.owner()
        if any(self.row[k]!=state[k] for k in ('run_id','cluster_uid','namespace_uid')):
            raise ValueError('attached-worker recovery ownership changed')
        return self.row

    def call(self,action):
        row=self.row['fence'];address=self.common.address(self.lab,self.row['failed_node'])
        code='import json,sys\n'+inspect.getsource(guest_fence)+'\nr=json.load(sys.stdin);print(json.dumps(guest_fence(r["request"],r["action"]),sort_keys=True))\n'
        return json.loads(self.lab.remote(address,['python3','-c',code],
            stdin=json.dumps({'request':row,'action':action}),timeout=600).stdout)

    def original_binding(self,business):
        row={}
        for kind,name,namespace in (('volumes.longhorn.io',business['volume'],NS),
                ('pv',business['pv'],''),('pvc','data',business['namespace'])):
            obj=self.lab.get(kind,name,namespace)
            if not obj:raise ValueError('attached-worker business binding disappeared')
            row[kind]=obj['metadata']['uid']
        expected={'volumes.longhorn.io':business['volume_uid'],'pv':business['pv_uid'],
                  'pvc':business['objects']['PersistentVolumeClaim/data']}
        if row!=expected:raise ValueError('attached-worker business binding changed')
        return row

    def force_old_pod(self,business):
        """An inaccessible node alone never grants this deletion authority."""
        original=self.row['original_pod'];previous=None
        for attempt in range(3):
            proof=self.call('probe')
            if proof.get('fenced') is not True or proof.get('kubepods_pids')!=[] or proof.get('host')!=self.row['fence']['host']:
                raise ValueError('force deletion requires current positive guest fencing')
            node=self.lab.get('node',self.row['failed_node'])
            if not node or node['metadata']['uid']!=self.row['nodes'][self.row['failed_node']]['uid']:
                raise ValueError('failed worker Kubernetes identity changed')
            if any(c.get('type')=='Ready' and c.get('status')=='True' for c in node.get('status',{}).get('conditions',[])):
                raise ValueError('force deletion requires the fenced worker NotReady')
            self.maintenance.generated(self.lab,business);self.original_binding(business)
            pod=self.lab.get('pod',original['name'],business['namespace'])
            if not pod or pod['metadata']['uid']!=original['uid'] or pod['spec'].get('nodeName')!=self.row['failed_node']:
                raise ValueError('force deletion refuses a changed or absent original workload Pod')
            version=pod['metadata']['resourceVersion']
            if version==previous:raise ValueError('force deletion Conflict did not yield a fresh resourceVersion')
            self.row['force_delete_intent']={'pod':original,'fence':proof};self.save()
            options={'apiVersion':'v1','kind':'DeleteOptions','gracePeriodSeconds':0,
                'preconditions':{'uid':pod['metadata']['uid'],'resourceVersion':version}}
            result=self.lab.kubectl('delete','--raw','/api/v1/namespaces/'+business['namespace']+'/pods/'+original['name'],
                '-f','-',stdin=json.dumps(options),check=False)
            self.row.setdefault('force_delete_attempts',[]).append({'preconditions':options['preconditions'],
                'rc':result.returncode,'stderr':result.stderr});self.save()
            if result.returncode==0:break
            pattern=(r'Error from server \(Conflict\): Operation cannot be fulfilled on Pod "'+re.escape(original['name'])+
                r'": the ResourceVersion in the precondition \('+re.escape(version)+
                r'\) does not match the ResourceVersion in record \(([^()\s]+)\)\. The object might have been modified')
            conflict=re.fullmatch(pattern,result.stderr.strip())
            if not conflict or conflict.group(1)==version:
                raise ValueError('force deletion failed without the exact resourceVersion Conflict; diagnostic retained')
            if attempt==2:raise ValueError('force deletion resourceVersion Conflict retry limit reached; diagnostic retained')
            previous=version
            print('LONGHORN_NODE_FORCE_DELETE_CONFLICT_RETRY attempt='+str(attempt+1),flush=True)
        def retired():
            current=self.lab.get('pod',original['name'],business['namespace'])
            if current and current['metadata']['uid']!=original['uid']:
                raise ValueError('force deletion original Pod name was replaced during retirement')
            return current is None
        self.lab.wait('node20-fenced-old-pod-retired',120,retired)

    def migrated(self,business):
        pod=self.maintenance.application(self.lab,business,excluded=self.row['failed_node'])
        if not pod or pod['metadata']['uid']==self.row['original_pod']['uid']:return None
        self.original_binding(business)
        volume=self.lab.get('volumes.longhorn.io',business['volume'],NS)
        if volume.get('status',{}).get('state')!='attached' or volume['status'].get('currentNodeID')!=pod['spec']['nodeName']:
            return None
        peers=[r for r in self.lab.get('replicas.longhorn.io',namespace=NS)['items']
            if r['spec'].get('volumeName')==business['volume'] and r['spec'].get('nodeID')!=self.row['failed_node']
            and r.get('status',{}).get('currentState')=='running' and r['spec'].get('healthyAt') and not r['spec'].get('failedAt')]
        if len({r['spec']['nodeID'] for r in peers})<2:return None
        engines=[e for e in self.lab.get('engines.longhorn.io',namespace=NS)['items']
            if e['spec'].get('volumeName')==business['volume'] and e['spec'].get('nodeID')==pod['spec']['nodeName']
            and e.get('status',{}).get('currentState')=='running']
        if len(engines)!=1 or any(engines[0]['status'].get('replicaModeMap',{}).get(r['metadata']['name'])!='RW' for r in peers):
            return None
        load('longhorn-v1-storage-faults.py').single_attachment(self.lab,business,pod['spec']['nodeName'])
        return pod

    def restore_host(self):
        if not self.row.get('fence_intent'):return
        address=self.common.address(self.lab,self.row['failed_node'])
        if not self.row.get('host_restored'):
            self.row['fence_restored']=self.call('restore');self.row['host_restored']=True;self.save()
        if self.row.get('reboot_intent'):
            def rebooted():
                current=self.common.host(self.lab,address,unavailable=True)
                if current is None or current['boot_id']==self.row['fence']['host']['boot_id']:return None
                if current['machine_id']!=self.row['fence']['host']['machine_id']:
                    raise ValueError('attached-worker reboot changed physical machine')
                return current
            self.row['new_host']=self.lab.wait('node20-actual-worker-reboot',1200,rebooted);self.save()

    def ready_with_owned_cordon(self,name,saved):
        """Permit only the lifecycle controller taint from our recorded cordon."""
        if name!=self.row.get('failed_node') or name not in self.row['touched']:
            raise ValueError('node failure refuses an unowned scheduling mutation')
        current=self.lab.get('node',name)
        if not current or current['metadata']['uid']!=saved['uid']:
            raise ValueError('node failure worker identity changed')
        if not any(c.get('type')=='Ready' and c.get('status')=='True'
                for c in current.get('status',{}).get('conditions',[])):
            return False
        expected=dict(saved,unschedulable=bool(current['spec'].get('unschedulable')))
        observed=current['spec'].get('taints',[])
        transient=[t for t in observed if t.get('key') in
            ('node.kubernetes.io/not-ready','node.kubernetes.io/unreachable')
            and t.get('effect') in ('NoSchedule','NoExecute')]
        taints=[t for t in observed if t not in transient]
        if taints!=saved['taints']:
            candidates=[t for t in taints if t.get('key')=='node.kubernetes.io/unschedulable']
            if len(candidates)!=1 or not expected['unschedulable']:
                raise ValueError('node failure refuses changed worker taints')
            taint=candidates[0]
            if (set(taint)-{'key','value','effect','timeAdded'} or taint.get('effect')!='NoSchedule'
                    or taint.get('value','')!=''):
                raise ValueError('node failure refuses noncanonical cordon taint')
            if 'timeAdded' in taint:
                timestamp=taint['timeAdded']
                if (not isinstance(timestamp,str) or not re.fullmatch(
                        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})',timestamp)):
                    raise ValueError('node failure refuses invalid cordon taint timeAdded')
                datetime.fromisoformat(timestamp[:-1]+'+00:00' if timestamp.endswith('Z') else timestamp)
            remaining=[t for t in taints if t is not taint]
            if remaining!=saved['taints']:
                raise ValueError('node failure refuses changed worker taints')
            # Preserve the API object, including optional value/timeAdded, for
            # common.node_restored's strict equality check on its fresh read.
            expected['taints']=taints
        # Node Ready and lifecycle taint removal converge asynchronously.
        # These taints are never accepted as the recovered policy.
        if transient:return False
        return self.common.node_restored(self.lab,name,expected)

    def restore_scheduling(self,name,saved,ready_label):
        self.lab.wait(ready_label,600,lambda:self.ready_with_owned_cordon(name,saved))
        self.maintenance.scheduling(self.lab,self.row,name,saved['unschedulable'])
        self.lab.wait('node20-original-scheduling-restored',180,lambda:
            self.common.node_restored(self.lab,name,saved))

    def clean(self,failed=False):
        load('longhorn-v1-reboot-relay.py').recover_current(self.lab)
        if not self.path.exists():return
        self.read();self.restore_host()
        for name,saved in self.row['nodes'].items():
            if name in self.row['touched']:
                if name!=self.row.get('failed_node'):
                    raise ValueError('node failure cleanup refuses an unowned scheduling mutation')
                self.restore_scheduling(name,saved,'node20-ready-before-scheduling')
            else:
                self.lab.wait('node20-ready-before-scheduling',600,lambda name=name,saved=saved:
                    self.common.node_restored(self.lab,name,saved))
        self.maintenance.clean(self.lab,self.pid,failed=False)
        if failed:
            self.row['recovered']=True;self.save()
            self.path.rename(self.path.with_name('longhorn-node-failure-recovered-'+self.row['token']+'.json'))
            print('LONGHORN_NODE_FAILURE_RECOVERY_SCOPED_CLEAN_PASS original_worker=true',flush=True)

    def business(self):
        lab=self.lab;m=self.maintenance
        if self.path.exists() or self.path.is_symlink():raise ValueError('recover the prior attached-worker fault first')
        m.empty(lab);lab.supervise_core(self.pid,'longhorn-setup');lab.installation_health()
        setting=lab.get('settings.longhorn.io','node-drain-policy',NS)
        if not setting or setting.get('value')!=m.POLICY:raise ValueError('node failure requires original drain policy')
        nodes=self.common.nodes(lab)
        if any(row['unschedulable'] or row['taints'] for row in nodes.values()):raise ValueError('node failure requires untainted schedulable receiving workers')
        state=lab.owner()
        self.row={k:state[k] for k in ('run_id','cluster_uid','namespace_uid')}
        self.row.update(token=secrets.token_hex(16),nodes=nodes,touched=[],passed=False,
            policy={'uid':setting['metadata']['uid'],'value':setting['value']},
            config_sha256=hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest(),shared=lab.shared_snapshot_identity())
        self.save()
        business,create=m.prepare(lab,self.row);m.scale(lab,business,3)
        old=lab.get('pod','writer',business['namespace']);lab.remove(old)
        lab.wait('node20-initial-bare-writer-retired',180,lambda:lab.get('pod','writer',business['namespace']) is None)
        template=lab.client_pod(business['namespace'],'unused','data',next(iter(nodes)))['spec']
        template.pop('nodeSelector');template['restartPolicy']='Always'
        template['affinity']={'nodeAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':{
            'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':list(nodes)}]}]}}}
        template['tolerations']=[{'key':'node.kubernetes.io/'+key,'operator':'Exists','effect':'NoExecute','tolerationSeconds':15}
            for key in ('not-ready','unreachable')]
        create({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'application','namespace':business['namespace']},
            'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'node20'}},
                'template':{'metadata':{'labels':{'app':'node20'}},'spec':template}}})
        app=lab.wait('node20-controlled-application',600,lambda:m.application(lab,business))
        m.check_data(lab,business,app['metadata']['name'],self.row);m.ledger(lab,business,app['metadata']['name'],self.row)
        engine=self.common.source(lab,business)
        if engine['node']!=app['spec']['nodeName']:raise ValueError('node failure did not select actual attached worker')
        node=engine['node'];address=self.common.address(lab,node)
        self.row.update(failed_node=node,original_pod={'name':app['metadata']['name'],'uid':app['metadata']['uid']},
            original_binding=self.original_binding(business))
        pods=[p for p in lab.get('pods',namespace='*')['items'] if p['spec'].get('nodeName')==node and not p['metadata'].get('deletionTimestamp')]
        if any(p['metadata']['namespace'] not in (NS,'kube-system',business['namespace']) for p in pods):
            raise ValueError('worker fence refuses another namespace consumer')
        if app['metadata']['uid'] not in {p['metadata']['uid'] for p in pods}:
            raise ValueError('worker fence does not include its actual attached application')
        self.row['fence']={'token':self.row['token'],'host':self.common.host(lab,address),'pod_uids':sorted(p['metadata']['uid'] for p in pods)}
        original=self.call('inspect');self.row['fence']['original']=original
        self.row['touched'].append(node);self.save();m.scheduling(lab,self.row,node,True)
        self.row['fence_intent']=True;self.save();self.row['fenced']=self.call('fence');self.save()
        def not_ready():
            obj=lab.get('node',node)
            if not obj or obj['metadata']['uid']!=nodes[node]['uid']:raise ValueError('node failure worker identity changed')
            return any(c.get('type')=='Ready' and c.get('status') in ('False','Unknown') for c in obj.get('status',{}).get('conditions',[]))
        lab.wait('node20-original-attached-worker-notready',180,not_ready)
        self.force_old_pod(business)
        after=lab.wait('node20-rwo-controller-rescheduled',900,lambda:self.migrated(business))
        self.call('probe');m.check_data(lab,business,after['metadata']['name'],self.row)
        self.row['migration']={'before':self.row['original_pod'],'after':{'name':after['metadata']['name'],
            'uid':after['metadata']['uid'],'node':after['spec']['nodeName']},'binding':self.original_binding(business)}
        self.row['recovery_payload']=self.row['token']+'-on-receiving-worker'
        self.row['recovery_digest']=lab.kubectl('-n',business['namespace'],'exec',after['metadata']['name'],'--','python3','-c',
            lab.payload_program(self.row['recovery_payload'])).stdout.strip()
        if self.row['recovery_digest']!=hashlib.sha256(self.row['recovery_payload'].encode()*262144).hexdigest():
            raise ValueError('node failure receiving worker fresh fsync acknowledgement differs')
        self.row.update(payload=self.row['recovery_payload'],digest=self.row['recovery_digest'])
        self.save();self.restore_host()
        self.row['reboot_intent']=True;self.save()
        load('longhorn-v1-reboot-relay.py').reboot(lab,self.pid,address,self.row['fence']['host'])
        self.restore_host()
        self.restore_scheduling(node,nodes[node],'node20-original-worker-ready-after-reboot')
        m.scale(lab,business,3)
        after=lab.wait('node20-recovered-controller-application',600,lambda:m.application(lab,business,excluded=node))
        if self.original_binding(business)!=self.row['original_binding']:
            raise ValueError('node failure changed original PVC/PV/volume binding after reboot')
        m.check_data(lab,business,after['metadata']['name'],dict(self.row,payload=self.row['recovery_payload'],digest=self.row['recovery_digest']))
        self.row['passed']=True;self.save()
        print('LONGHORN_NODE_FAILURE_OBSERVED '+json.dumps({
            'fence_scope':'guest-runtime','infrastructure_power_fence':False,'original_host':self.row['fence']['host'],
            'new_host':self.row['new_host'],'migration':self.row['migration'],'ledger':self.row['ledger']['summary'],
            'fresh_sha256':self.row['recovery_digest'],'toleration_seconds':15},sort_keys=True),flush=True)
        # The normal maintenance cleanup must verify the most recent bytes,
        # while retaining the immutable pre-fault acknowledgement ledger.
        self.clean();m.finish(lab)
        print('LONGHORN_NODE_FAILURE_SCOPED_CLEAN_PASS original_scheduling=true worker_ready=true',flush=True)
        print('LONGHORN_NODE_FAILURE_PASS',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('business','clean'))
    parser.add_argument('--relay-pid',type=int,required=True);args=parser.parse_args()
    lab=load('longhorn-lab.py');lab.require_lease()
    if args.relay_pid<=0:parser.error('supervised relay PID required')
    probe=Failure(lab,args.relay_pid)
    if args.action=='business':probe.business()
    else:probe.clean(failed=True)


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda number,frame:sys.exit(128+number))
    main()
