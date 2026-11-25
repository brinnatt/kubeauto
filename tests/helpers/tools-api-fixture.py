#!/usr/bin/env python3
"""Real isolated API control planes for context and delete contracts; no CNI/node claim."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time

PINS = {'kube-apiserver': '88619f24329ace76d9c2f7c2fbdd5a8a28831e711df403d7aaca93ece51254fb',
        'kubectl': '29818b6729dc9262b692307470d0558d027725e738be5dd47c760261868a2634',
        'etcd': '769222687e03cb2ee0c420193a5012d4ff2a35a47e8f27ae9540dd2acfa3ac49',
        'calicoctl': '4dd81052745b0358d29505d3759bf5e83b0cb0e606de70760072e3edb6084d4e'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def assert_refusal(result, expected):
    if result.returncode == 0 or expected not in result.stdout + result.stderr:
        raise ValueError('TOOLS_API_REFUSAL_MISSING ' + expected)


def api_arguments(work, name, port):
    return ['kube-apiserver','--bind-address=127.0.0.1','--advertise-address=127.0.0.1','--secure-port='+str(port),
            '--etcd-servers=http://127.0.0.1:12379','--etcd-prefix=/tools-api-'+name,
            '--service-cluster-ip-range=10.250.0.0/24','--authorization-mode=RBAC',
            '--client-ca-file='+str(work/'ca.crt'),'--tls-cert-file='+str(work/'server.crt'),
            '--tls-private-key-file='+str(work/'server.key'),'--service-account-key-file='+str(work/'ca.crt'),
            '--service-account-signing-key-file='+str(work/'ca.key'),'--service-account-issuer=https://kubernetes.default.svc']


def verify_ports(ports=(12379, 12380, 16443, 16444)):
    for port in ports:
        with socket.socket() as probe:
            # Match the servers' reusable bind: TIME_WAIT is not a listener.
            # A live listening socket still rejects this bind (no SO_REUSEPORT).
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))


def cleanup_owned(work, processes, streams):
    for process in reversed(processes):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for process in processes:
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    for stream in streams:
        stream.close()
    shutil.rmtree(work)
    verify_ports()
    print('TOOLS_API_SCOPED_CLEAN_PASS processes=0 pki=removed ports=free', flush=True)


def backup_resource_scenario(root, work, command, apply, get, kubectl):
    tool = root/'KubeBackupCli'
    namespace, target = 'tools-kb-source', 'tools-kb-restored'
    apply('a', {'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}})
    label = {'owner':'tools-backup'}
    objects = [
        {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'settings'},'data':{'DOMAIN':'api.'+namespace}},
        {'apiVersion':'v1','kind':'Secret','metadata':{'name':'credential'},'stringData':{'TOKEN':'tools-fixture-secret-only'}},
        {'apiVersion':'v1','kind':'Service','metadata':{'name':'service'},'spec':{'selector':label,'ports':[{'port':80}]}},
        {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'app'},'spec':{'replicas':1,
         'selector':{'matchLabels':label},'template':{'metadata':{'labels':label},'spec':{'containers':[{
         'name':'app','image':'example.invalid/fixture:v1','env':[{'name':'DOMAIN','value':'api.'+namespace}]}]}}}},
        {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'Role','metadata':{'name':'reader'},
         'rules':[{'apiGroups':[''],'resources':['configmaps'],'verbs':['get']}]},
        {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding','metadata':{'name':'binding'},
         'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'reader'},
         'subjects':[{'kind':'ServiceAccount','name':'default','namespace':namespace}]}]
    for obj in objects:
        obj['metadata'].update(namespace=namespace, labels=label)
        if obj['kind']=='ConfigMap':
            obj['metadata'].update(finalizers=['tools.example.com/fixture'],
                ownerReferences=[{'apiVersion':'v1','kind':'ConfigMap','name':'foreign-owner','uid':'00000000-0000-0000-0000-000000000001'}])
        apply('a',obj)
    common = [str(tool),'backup','--context','a','--output-dir',str(work/'resources')]
    def backup(name, *extra):
        return command(backup_arguments(common, namespace, name, extra), 'kb-resources-'+name)
    resources='configmaps,secrets,services,deployments,roles,rolebindings'
    result=backup('primary','--resources',resources,'--label-selector','owner=tools-backup','--max-workers','8','--tar')
    if 'tools-fixture-secret-only' in result.stdout+result.stderr or 'dG9vbHMtZml4dHVyZS1zZWNyZXQtb25seQ==' in result.stdout+result.stderr:
        raise ValueError('TOOLS_KB_SECRET_IN_PRODUCT_LOG')
    saved=work/'resources/primary'
    with tarfile.open(str(saved)+'.tar.gz') as archive:
        if not any(Path(m.name).name=='backup-metadata.json' for m in archive.getmembers()):
            raise ValueError('TOOLS_KB_ARCHIVE_METADATA_MISSING')
    for kind, name in (('ConfigMap','settings'),('Secret','credential'),('Service','service'),('Deployment','app'),('Role','reader'),('RoleBinding','binding')):
        path=saved/namespace/kind/(kind+'-'+name+'.yaml')
        if not path.is_file():raise ValueError('TOOLS_KB_RESOURCE_MISSING '+kind)
        obj=json.loads(kubectl('a','create','--dry-run=client','--validate=false','-f',str(path),'-o','json').stdout)
        if {'uid','resourceVersion','managedFields','creationTimestamp','ownerReferences','finalizers'} & obj['metadata'].keys():
            raise ValueError('TOOLS_KB_METADATA_NOT_CLEAN '+kind)
        if kind=='Service' and 'clusterIP' in obj.get('spec',{}):raise ValueError('TOOLS_KB_CLUSTER_IP_NOT_CLEAN')
    backup('include','--resources','configmaps','--include-names','settings','--field-selector','metadata.name=settings')
    backup('exclude','--resources','configmaps','--exclude-names','settings')
    if list((work/'resources/exclude').rglob('ConfigMap-settings.yaml')):raise ValueError('TOOLS_KB_EXCLUDE_IGNORED')
    backup('all','--all-namespaces','--resources','configmaps','--include-names','settings')
    backup('dry','--resources','configmaps','--dry-run')
    if (work/'resources/dry').exists():raise ValueError('TOOLS_KB_BACKUP_DRY_MUTATED')
    crd='kbwidgets.tools.example.com'
    apply('a', {'apiVersion':'apiextensions.k8s.io/v1','kind':'CustomResourceDefinition','metadata':{'name':crd},
               'spec':{'group':'tools.example.com','scope':'Namespaced',
               'names':{'plural':'kbwidgets','singular':'kbwidget','kind':'KbWidget'},
               'versions':[{'name':'v1','served':True,'storage':True,'schema':{'openAPIV3Schema':{'type':'object',
               'properties':{'spec':{'type':'object','properties':{'value':{'type':'string'}}}}}}}]}})
    kubectl('a','wait','--for=condition=Established','crd/'+crd,'--timeout=60s')
    apply('a',{'apiVersion':'tools.example.com/v1','kind':'KbWidget','metadata':{'name':'fixture-widget','namespace':namespace},'spec':{'value':'readback'}})
    backup('crd','--resources','configmaps','--include-crds','--include-names',crd+',fixture-widget')
    crd_saved=work/'resources/crd'
    if not list(crd_saved.rglob('KbWidget-fixture-widget.yaml')) or not list(crd_saved.rglob('CustomResourceDefinition-kbwidgets_tools_example_com.yaml')):
        raise ValueError('TOOLS_KB_CRD_ARCHIVE_MISSING')
    restore=[tool,'restore','--context','a','--backup-dir',saved,'--namespace-mapping',namespace+'='+target,
             '--create-namespaces','--env-mapping','NEW_VALUE=present','--image-mapping','example.invalid/=example.invalid/restored/',
             '--merge-patch','{"spec":{"replicas":2}}','--merge-patch-kind','Deployment']
    command([*restore,'--dry-run'],'kb-resource-restore-dry')
    result=kubectl('a','get','namespace',target,check=False); assert_refusal(result,'NotFound')
    command(restore,'kb-resource-restore')
    command([tool,'restore','--context','a','--backup-dir',crd_saved,'--namespace-mapping',namespace+'='+target,
             '--skip-crds','--skip-cluster-scoped','--create-namespaces'],'kb-crd-restore')
    if get('a','kbwidget','fixture-widget',target)['spec']['value']!='readback':raise ValueError('TOOLS_KB_CR_READBACK_MISMATCH')
    dep=get('a','deployment','app',target)
    container=dep['spec']['template']['spec']['containers'][0]
    if dep['spec']['replicas']!=2 or container['image']!='example.invalid/restored/fixture:v1':raise ValueError('TOOLS_KB_REWRITE_MISMATCH')
    values={e['name']:e.get('value') for e in container['env']}
    if values.get('DOMAIN')!='api.'+target or values.get('NEW_VALUE')!='present':raise ValueError('TOOLS_KB_ENV_MISMATCH')
    for kind,name in (('secret','credential'),('configmap','settings'),('service','service'),('role','reader'),('rolebinding','binding')):get('a',kind,name,target)
    if get('a','secret','credential',target)['data'] != get('a','secret','credential',namespace)['data']:raise ValueError('TOOLS_KB_SECRET_READBACK_MISMATCH')
    bad=saved/namespace/'ConfigMap/ConfigMap-invalid.yaml'
    bad.write_text(json.dumps({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'invalid_name','namespace':namespace}}))
    result=command(restore,'kb-partial-refusal',check=False);assert_refusal(result,'422')
    bad.unlink();command(restore,'kb-partial-retry')
    get('a','deployment','app',target)
    for name,argv,cause in (('path',[*common,'--backup-name','../escape'],'备份名称'),
                            ('env',[tool,'restore','--context','a','--backup-dir',saved,'--env-mapping','BAD-NAME=value'],'BAD-NAME')):
        result=command(argv,'kb-refuse-'+name,check=False)
        if result.returncode==0:raise ValueError('TOOLS_KB_INVALID_INPUT_ACCEPTED '+name)
    # An immutable selector conflict must fail without replacing the live Deployment.
    path=saved/namespace/'Deployment/Deployment-app.yaml'
    obj=json.loads(kubectl('a','create','--dry-run=client','--validate=false','-f',str(path),'-o','json').stdout)
    obj['spec']['selector']['matchLabels']={'different':'selector'}
    obj['spec']['template']['metadata']['labels']={'different':'selector'}
    path.write_text(json.dumps(obj))
    result=command(restore,'kb-immutable-refusal',check=False);assert_refusal(result,'422')
    if get('a','deployment','app',target)['spec']['selector']['matchLabels']!=label:raise ValueError('TOOLS_KB_CONFLICT_REPLACED_OBJECT')
    print('TOOLS_KB_RESOURCES_API_PASS resources=6 crd=readback archive=clean mappings=readback partial=retry immutable=refused secret=not-logged',flush=True)


def backup_arguments(common, namespace, name, extra):
    return [*common, *([] if '--all-namespaces' in extra else ['--namespace', namespace]),
            '--backup-name', name, *extra]


def main():
    root = Path.cwd()
    if root.parent != Path('/tmp') or not root.name.startswith('tools-api-'):
        raise ValueError('TOOLS_API_SCOPE_REFUSED')
    work, logs = root/'fixture', root/'diagnostics'
    if work.exists():
        raise ValueError('TOOLS_API_FOREIGN_FIXTURE_REFUSED')
    for name, expected in PINS.items():
        if sha('/usr/local/bin/'+name) != expected:
            raise ValueError('TOOLS_API_BINARY_REFUSED '+name)
    verify_ports()
    work.mkdir(mode=0o700); logs.mkdir(mode=0o700)
    processes, streams = [], []
    env = dict(os.environ, KUBECONFIG=str(work/'config'), KUBERNETES_SERVICE_HOST='127.0.0.1',
               KUBERNETES_SERVICE_PORT='1', DATASTORE_TYPE='kubernetes')
    def command(argv, name, input=None, check=True):
        result = subprocess.run([str(x) for x in argv], env=env, input=input, capture_output=True, text=True, timeout=90)
        (logs/(name+'.log')).write_text(result.stdout+result.stderr)
        if check and result.returncode:
            raise ValueError('TOOLS_API_COMMAND_REFUSED '+name+'\n'+(result.stdout+result.stderr)[-3500:])
        return result
    def start(argv, name):
        stream = (logs/(name+'.log')).open('w'); streams.append(stream)
        process = subprocess.Popen([str(x) for x in argv], env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(process)
    def kubectl(context, *args, **kwargs):
        return command(['kubectl', '--request-timeout=5s', '--kubeconfig', work/'config', '--context', context, *args],
                       'kubectl-'+str(time.monotonic_ns()), **kwargs)
    def apply(context, obj):
        kubectl(context, 'apply', '-f', '-', input=json.dumps(obj))
    def get(context, kind, name, namespace=None):
        return json.loads(kubectl(context, 'get', kind, name, *(['-n', namespace] if namespace else []), '-o', 'json').stdout)
    def interrupted(signum, frame):
        raise InterruptedError('TOOLS_API_INTERRUPTED '+str(signum))
    for signum in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(signum,interrupted)
    try:
        # Private certificates never leave the fixture or enter the diagnostic archive.
        command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=tools-api-ca',
                 '-keyout',work/'ca.key','-out',work/'ca.crt'], 'ca')
        (work/'server.ext').write_text('subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n')
        for name, subject in (('server','/CN=127.0.0.1'),('admin','/CN=tools-admin/O=system:masters'),('limited','/CN=tools-limited')):
            command(['openssl','req','-new','-newkey','rsa:2048','-nodes','-subj',subject,
                     '-keyout',work/(name+'.key'),'-out',work/(name+'.csr')],name+'-request')
            args=['openssl','x509','-req','-in',work/(name+'.csr'),'-CA',work/'ca.crt','-CAkey',work/'ca.key',
                  '-CAcreateserial','-days','1','-out',work/(name+'.crt')]
            if name=='server': args += ['-extfile',work/'server.ext']
            command(args,name+'-sign')
        config={'apiVersion':'v1','kind':'Config','clusters':[], 'contexts':[], 'users':[], 'current-context':'b'}
        for name, port in (('a',16443),('b',16444)):
            config['clusters'].append({'name':name,'cluster':{'server':'https://127.0.0.1:'+str(port),'certificate-authority':str(work/'ca.crt')}})
            config['contexts'].append({'name':name,'context':{'cluster':name,'user':'admin'}})
        for name in ('admin','limited'):
            config['users'].append({'name':name,'user':{'client-certificate':str(work/(name+'.crt')),'client-key':str(work/(name+'.key'))}})
        config['contexts'].append({'name':'limited','context':{'cluster':'a','user':'limited'}})
        (work/'config').write_text(json.dumps(config)); (work/'config').chmod(0o600)
        start(['etcd','--name=tools-api','--data-dir='+str(work/'etcd'),'--listen-client-urls=http://127.0.0.1:12379',
               '--advertise-client-urls=http://127.0.0.1:12379','--listen-peer-urls=http://127.0.0.1:12380',
               '--initial-advertise-peer-urls=http://127.0.0.1:12380','--initial-cluster=tools-api=http://127.0.0.1:12380'], 'etcd')
        for name, port in (('a',16443),('b',16444)):
            start(api_arguments(work,name,port),'apiserver-'+name)
        for context in ('a','b'):
            for attempt in range(60):
                if any(p.poll() is not None for p in processes):
                    raise ValueError('TOOLS_API_PROCESS_EXITED; inspect apiserver/etcd diagnostic logs')
                ready=kubectl(context, 'get', '--raw=/readyz', check=False)
                if ready.returncode==0 and ready.stdout.strip()=='ok':break
                if attempt%10==0:print('TOOLS_API_HEARTBEAT context='+context+' step=readyz attempt='+str(attempt),flush=True)
                time.sleep(1)
            else:raise ValueError('TOOLS_API_READY_TIMEOUT '+context)
        print('TOOLS_API_READY_PASS kubernetes=1.36.6 etcd=3.6.16 clusters=2',flush=True)
        namespace='tools-boundary'
        for context, value in (('a','correct-a'),('b','foreign-b')):
            apply(context, {'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}})
            apply(context, {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'selected','namespace':namespace},'data':{'identity':value}})
        backup=root/'KubeBackupCli'
        result=command([backup,'backup','--context','a','--namespace',namespace,'--resources','configmaps',
                        '--backup-name','selected','--output-dir',work/'backups'], 'kb-backup')
        saved=work/'backups/selected'
        files=list(saved.rglob('ConfigMap-selected.yaml'))
        if len(files)!=1 or 'correct-a' not in files[0].read_text() or 'foreign-b' in files[0].read_text():
            raise ValueError('TOOLS_KB_SELECTED_WRONG_CLUSTER')
        command([backup,'restore','--context','a','--backup-dir',saved,'--namespace-mapping',namespace+'=tools-restored',
                 '--create-namespaces'], 'kb-restore')
        if get('a','configmap','selected','tools-restored')['data']['identity']!='correct-a':
            raise ValueError('TOOLS_KB_RESTORE_READBACK_MISMATCH')
        if get('b','configmap','selected',namespace)['data']['identity']!='foreign-b':
            raise ValueError('TOOLS_KB_FOREIGN_CLUSTER_MUTATED')
        if kubectl('b','get','namespace','tools-restored',check=False).returncode==0:
            raise ValueError('TOOLS_KB_RESTORED_TO_FOREIGN_CLUSTER')
        result=command([backup,'backup','--context','missing','--namespace',namespace,'--resources','configmaps',
                        '--backup-name','unknown','--output-dir',work/'backups'], 'kb-unknown',check=False)
        assert_refusal(result,'context')
        result=command([backup,'backup','--context','limited','--namespace',namespace,'--resources','configmaps',
                        '--backup-name','denied','--output-dir',work/'backups'], 'kb-denied',check=False)
        assert_refusal(result,'403')
        print('TOOLS_KB_CONTEXT_RESTORE_PASS default=b explicit=a frozen=1 readback=correct-a foreign=unchanged',flush=True)
        print('TOOLS_KB_CONTEXT_REFUSAL_PASS unknown=nonzero rbac=403',flush=True)
        backup_resource_scenario(root, work, command, apply, get, kubectl)
        # Register exact upstream schemas, not success-returning API stubs.
        for crd in json.loads((root/'calico-crds.json').read_text()):
            apply('a',crd)
            kubectl('a','wait','--for=condition=Established','crd/'+crd['metadata']['name'],'--timeout=60s')
        apply('a',{'apiVersion':'crd.projectcalico.org/v1','kind':'ClusterInformation',
                   'metadata':{'name':'default'},'spec':{'calicoVersion':'v3.32.4','datastoreReady':True}})
        calico=root/'CalicoPolicyCli'
        def seed():
            apply('a',{'apiVersion':'crd.projectcalico.org/v1','kind':'GlobalNetworkPolicy','metadata':{'name':'tools-owned'},'spec':{'selector':'all()','types':['Ingress']}})
            for name, labels in (('tools-owned',{'kubeauto.calico/host-port-lockdown':'true'}),('tools-foreign',{'owner':'foreign'})):
                apply('a',{'apiVersion':'crd.projectcalico.org/v1','kind':'HostEndpoint','metadata':{'name':name,'labels':labels},
                           'spec':{'node':'fixture','interfaceName':'eth0','expectedIPs':['127.0.0.1']}})
            apply('a',{'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'tools-owned','namespace':namespace},
                       'spec':{'podSelector':{},'policyTypes':['Ingress']}})
        args=[str(calico),'--context','a','--executor','kubectl','--no-log-file','delete','--traffic-layer','both',
              '--policy-name','tools-owned','--k8s-np-name','tools-owned','-n',namespace,'--delete-hostendpoints']
        seed(); command(args,'calico-delete')
        command(args,'calico-delete-idempotent')
        get('a','hostendpoints.crd.projectcalico.org','tools-foreign')
        for resource in ('globalnetworkpolicies.crd.projectcalico.org','hostendpoints.crd.projectcalico.org'):
            if kubectl('a','get',resource,'tools-owned',check=False).returncode==0:raise ValueError('TOOLS_CALICO_DELETE_LEFT_RESOURCE')
        if kubectl('a','get','networkpolicy','tools-owned','-n',namespace,check=False).returncode==0:raise ValueError('TOOLS_CALICO_DELETE_LEFT_NETPOL')
        seed()
        # Delegate all successful calls to the actual client. Only the first
        # delete fails, so later objects must still exist when the CLI returns.
        wrapper=work/'fail-kubectl'
        wrapper.write_text('#!/bin/sh\nfor a do if [ "$a" = delete ]; then echo injected-api-delete-failure >&2; exit 23; fi; done\nexec /usr/local/bin/kubectl "$@"\n')
        wrapper.chmod(0o755)
        result=command(args[:1]+['--kubectl',str(wrapper)]+args[1:],'calico-refusal',check=False)
        assert_refusal(result,'injected-api-delete-failure')
        get('a','globalnetworkpolicies.crd.projectcalico.org','tools-owned')
        get('a','hostendpoints.crd.projectcalico.org','tools-owned')
        get('a','networkpolicy','tools-owned',namespace)
        command(args,'calico-retry')
        get('a','hostendpoints.crd.projectcalico.org','tools-foreign')
        for executor in ('kubectl','calicoctl'):
            for layer in ('host','pod','both'):
                seed()
                argv=[executor if a=='kubectl' else layer if a=='both' else a for a in args]
                command(argv,'calico-'+executor+'-'+layer)
                # The successful command's unaffected layer remains real API data.
                if layer=='host':get('a','networkpolicy','tools-owned',namespace)
                if layer=='pod':get('a','globalnetworkpolicies.crd.projectcalico.org','tools-owned')
                command(args,'calico-scoped-reset-'+executor+'-'+layer)
                get('a','hostendpoints.crd.projectcalico.org','tools-foreign')
        for executor,layer,failure in (('kubectl','pod','delete'),('kubectl','both','delete'),
                                       ('calicoctl','both','delete'),('calicoctl','host','get')):
            seed()
            wrapper=work/('fail-'+executor+'-'+failure)
            wrapper.write_text('#!/bin/sh\nfor a do if [ "$a" = '+failure+' ]; then echo injected-'+failure+'-failure >&2; exit 23; fi; done\nexec /usr/local/bin/'+executor+' "$@"\n')
            wrapper.chmod(0o755)
            argv=[executor if a=='kubectl' else layer if a=='both' else a for a in args]
            argv=argv[:1]+['--'+executor,str(wrapper)]+argv[1:]
            result=command(argv,'calico-fault-'+executor+'-'+layer+'-'+failure,check=False)
            assert_refusal(result,'injected-'+failure+'-failure')
            get('a','networkpolicy','tools-owned',namespace)
            get('a','hostendpoints.crd.projectcalico.org','tools-owned')
            if failure=='delete':get('a','globalnetworkpolicies.crd.projectcalico.org','tools-owned')
            command(args,'calico-fault-retry-'+executor+'-'+layer+'-'+failure)
            get('a','hostendpoints.crd.projectcalico.org','tools-foreign')
        print('TOOLS_CALICO_DELETE_API_PASS real_crds=3.32.4 executors=2 layers=3 faults=4 retry=ok foreign=unchanged',flush=True)
    finally:
        cleanup_owned(work, processes, streams)


if __name__=='__main__':
    main()
