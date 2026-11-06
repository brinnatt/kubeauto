#!/usr/bin/env python3
"""Owned EFK fixtures for proving shared-ECK business isolation."""
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import time


@contextmanager
def logging_connection(lab):
    k,es=lab.api(),lab.load_es();service='logging-es-http';namespace='logging'
    cert=k.get('secret','logging-es-http-certs-public',namespace)['data']
    admin=k.get('secret','logging-es-elastic-user',namespace)['data']
    context=ssl.create_default_context(cadata=base64.b64decode(cert.get('ca.crt') or cert['tls.crt']).decode())
    auth=base64.b64encode(b'elastic:'+base64.b64decode(admin['elastic'])).decode()
    with tempfile.TemporaryFile(mode='w+') as log:
        process=subprocess.Popen(k.command+['-n',namespace,'port-forward','service/'+service,':9200','--address=127.0.0.1'],stdout=log,stderr=log,text=True)
        try:
            deadline=time.monotonic()+30;port=None
            while time.monotonic()<deadline and process.poll() is None:
                log.seek(0);match=re.search(r'Forwarding from 127\.0\.0\.1:([0-9]+)',log.read())
                if match:port=int(match[1]);break
                time.sleep(.2)
            if port is None:raise ValueError('EFK fixture HTTPS tunnel did not become ready')
            def request(method,path,body=None):
                conn=es.TunnelHTTPS(service+'.'+namespace+'.svc',port,timeout=120,context=context)
                try:
                    conn.request(method,path,body=json.dumps(body) if body is not None else None,
                                 headers={'Authorization':'Basic '+auth,'Content-Type':'application/json'})
                    response=conn.getresponse();raw=response.read()
                    if response.status>=300:raise ValueError('EFK fixture API refused status='+str(response.status)+es.api_error_types(raw))
                    return json.loads(raw) if raw else {}
                finally:conn.close()
            yield request
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)


def fixture_image(lab, key):
    records=json.loads((lab.BASE/'tests/evidence/elasticsearch-images-20261103.json').read_text())['images']
    row=next(row for row in records if row['key']==key)
    return row['talkedu_ref'].rsplit(':',1)[0]+'@'+row['digest']


def minio_job(lab, script, label):
    k=lab.api();name='es-gate-minio-'+secrets.token_hex(6)
    meta={'name':name,'namespace':'logging','labels':lab.LABELS}
    k.apply([{'apiVersion':'batch/v1','kind':'Job','metadata':meta,'spec':{'backoffLimit':0,'activeDeadlineSeconds':120,
        'template':{'metadata':{'labels':lab.LABELS},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,
            'containers':[{'name':'mc','image':fixture_image(lab,'brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z'),
                'command':['/bin/sh','-ec','mc alias set gate http://minio.logging.svc:9000 "$ACCESS" "$KEY" >/dev/null\n'+script],
                'env':[{'name':name,'valueFrom':{'secretKeyRef':{'name':'es-gate-minio','key':key}}} for name,key in (('ACCESS','username'),('KEY','password'))]}]}}}}])
    uid=k.get('job',name,'logging')['metadata']['uid']
    try:k.run('-n','logging','wait','--for=condition=Complete','job/'+name,'--timeout=150s',timeout=180)
    finally:
        primary=sys.exc_info()[1]
        try:
            obj=k.get('job',name,'logging')
            if not obj or obj['metadata']['uid']!=uid:raise ValueError('EFK backend client identity changed')
            k.run('-n','logging','delete','job',name,'--wait=true','--timeout=120s',timeout=150)
        except Exception as error:
            if primary is None:raise
            print('ELASTICSEARCH_COEXISTENCE_CLIENT_CLEANUP_FAIL reason='+type(error).__name__,file=sys.stderr)
    print('ELASTICSEARCH_COEXISTENCE_BACKEND_PASS check='+label,flush=True)


def fixtures(lab):
    k=lab.api()
    if k.get('namespace','logging'):raise ValueError('coexistence refuses a retained logging namespace')
    labels=dict(lab.LABELS,**{'kubeauto.io/component':'logging','kubeauto.io/logging-solution':'efk'})
    k.apply([{'apiVersion':'v1','kind':'Namespace','metadata':{'name':'logging','labels':labels}}]);lab.bind_namespace('logging')
    user,key='eslogging'+secrets.token_hex(6),secrets.token_urlsafe(32)
    metadata=lambda name:{'name':name,'namespace':'logging','labels':labels}
    k.apply([{'apiVersion':'v1','kind':'Secret','metadata':metadata('es-gate-minio'),'type':'Opaque','stringData':{'username':user,'password':key}},
        {'apiVersion':'v1','kind':'Secret','metadata':metadata('logging-snapshot-s3'),'type':'Opaque',
         'stringData':{'s3.client.default.access_key':user,'s3.client.default.secret_key':key}},
        {'apiVersion':'v1','kind':'Secret','metadata':metadata('logging-efk-writer'),'type':'Opaque',
         'stringData':{'username':'fluent-bit','password':secrets.token_urlsafe(32)}},
        {'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':metadata('es-gate-minio'),'spec':{'accessModes':['ReadWriteOnce'],
         'storageClassName':'local-path','resources':{'requests':{'storage':'2Gi'}}}},
        {'apiVersion':'v1','kind':'Service','metadata':metadata('minio'),'spec':{'selector':{'app':'es-gate-minio'},'ports':[{'name':'s3','port':9000}]}},
        {'apiVersion':'apps/v1','kind':'Deployment','metadata':metadata('minio'),'spec':{'replicas':1,'selector':{'matchLabels':{'app':'es-gate-minio'}},
         'template':{'metadata':{'labels':{'app':'es-gate-minio'}},'spec':{'automountServiceAccountToken':False,
             'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':'es-gate-minio'}}],
             'containers':[{'name':'minio','image':fixture_image(lab,'brinnatt/minio:RELEASE.2025-04-08T15-41-24Z'),
                 'args':['server','/data'],'ports':[{'containerPort':9000}],'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'memory':'512Mi'}},
                 'readinessProbe':{'httpGet':{'path':'/minio/health/ready','port':9000}},'volumeMounts':[{'name':'data','mountPath':'/data'}],
                 'env':[{'name':name,'valueFrom':{'secretKeyRef':{'name':'es-gate-minio','key':key}}} for name,key in (('MINIO_ROOT_USER','username'),('MINIO_ROOT_PASSWORD','password'))]}]}}}}])
    k.run('-n','logging','rollout','status','deployment/minio','--timeout=5m',timeout=330)
    minio_job(lab,'mc mb gate/logging-snapshots >/dev/null','independent-bucket')
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary)
        lab.execute(['openssl','req','-x509','-nodes','-newkey','rsa:2048','-days','2','-keyout',root/'tls.key','-out',root/'tls.crt',
                     '-subj','/CN=kibana.logging.test','-addext','subjectAltName=DNS:kibana.logging.test'])
        k.apply([{'apiVersion':'v1','kind':'Secret','metadata':metadata('logging-kibana-ingress-tls'),'type':'kubernetes.io/tls',
                  'data':{name:base64.b64encode((root/name).read_bytes()).decode() for name in ('tls.crt','tls.key')}}])


def capacity(lab):
    """Admit simultaneous EFK and upgrade fixtures before creating either."""
    k, es = lab.api(), lab.load_es()
    free = {obj['metadata']['name']: es.quantity(obj['status']['allocatable']['memory'])
            for obj in k.get('nodes')['items'] if obj['metadata']['name'] in lab.NODES}
    for pod in k.get('pods')['items']:
        node = pod['spec'].get('nodeName')
        if node not in free or pod.get('status',{}).get('phase') in ('Succeeded','Failed'):
            continue
        def memory(container):return es.quantity(container.get('resources',{}).get('requests',{}).get('memory','0'))
        free[node] -= max(sum(map(memory,pod['spec'].get('containers',[]))),
                          max([memory(c) for c in pod['spec'].get('initContainers',[])]+[0]))
        free[node] -= es.quantity(pod['spec'].get('overhead',{}).get('memory','0'))
    candidates = sorted(free,key=lambda node:(-free[node],node))
    logging_nodes = [node for node in candidates if free[node]>=es.quantity('8Gi')][:3]
    original = lab.configuration()
    try:
        c = lab.selected_config('compact')
    finally:
        lab.configuration(**original)
    upgrade_nodes = [node for node in c['elasticsearch_data_nodes'] if node not in logging_nodes
                     and free.get(node,0)>=es.quantity(c['elasticsearch_data_memory'])]
    if len(logging_nodes)!=3 or len(upgrade_nodes)<3:
        raise ValueError('coexistence capacity cannot admit EFK plus an independent three-member upgrade fixture')
    remaining = sum(free.values())-es.quantity('24Gi')-3*es.quantity(c['elasticsearch_data_memory'])
    if remaining < es.quantity('4Gi'):
        raise ValueError('coexistence capacity lacks Kibana and control-plane headroom')
    result = {'logging_nodes':logging_nodes,'upgrade_nodes':upgrade_nodes[:3]}
    lab.atomic(lab.STATE/'coexistence-capacity.json',result)
    print('ELASTICSEARCH_COEXISTENCE_CAPACITY_PASS '+json.dumps(result,sort_keys=True),flush=True)


def identities(lab):
    k=lab.api()
    return {'pvc':{p['metadata']['name']:p['metadata']['uid'] for p in k.get('pvc',namespace='logging')['items']},
            'secrets':{name:{'uid':k.get('secret',name,'logging')['metadata']['uid'],'data':k.get('secret',name,'logging')['data']}
                       for name in ('logging-snapshot-s3','logging-efk-writer','logging-kibana-ingress-tls')},
            'operator_uid':k.get('statefulset','elastic-operator','elastic-system')['metadata']['uid']}


def verify(lab, record):
    if identities(lab)!=record['identities']:raise ValueError('coexistence changed EFK claims/customer credentials/shared Operator')
    with logging_connection(lab) as request:
        if request('GET','/')['cluster_uuid']!=record['cluster_uuid']:raise ValueError('coexistence changed the EFK data cluster identity')
        request('POST','/'+record['index']+'/_refresh')
        if request('GET','/'+record['index']+'/_doc/sentinel')['_source']!=record['document'] or request('GET','/'+record['index']+'/_count')['count']!=1:
            raise ValueError('coexistence lost or altered the EFK business sentinel')
        snapshot=request('GET','/_snapshot/logging-s3/'+record.get('snapshot','coexistence'))['snapshots'][0]
        if snapshot['uuid']!=record['snapshot_uuid'] or snapshot['state']!='SUCCESS':raise ValueError('coexistence changed the independent EFK backup')


def unload_efk(lab):
    k=lab.api()
    # EFK uses the established native CR retirement path. The whole-CR
    # DeleteOnScaledownOnly policy retains its claims; customer Secrets and
    # the independent repository are outside these two delete requests.
    candidates=[]
    for kind,plural,group in (('kibana','kibanas','kibana'),('elasticsearch','elasticsearches','elasticsearch')):
        # Ordinary kubectl JSON output omits the ownership fields we inspect.
        raw=k.run('-n','logging','get',kind,'logging','--ignore-not-found','-o','json','--show-managed-fields=true')
        obj=json.loads(raw) if raw.strip() else None
        if (not obj or obj['metadata'].get('labels',{}).get('app.kubernetes.io/managed-by')!='kubeauto' or
                obj['metadata'].get('labels',{}).get('kubeauto.io/component')!='logging-efk' or
                not any(row.get('manager')=='kubeauto-logging-efk' for row in obj['metadata'].get('managedFields',[]))):
            raise ValueError('EFK retirement ownership is unproven')
        candidates.append((kind,plural,group,obj))
    for kind,plural,group,obj in candidates:
        options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':obj['metadata']['uid'],'resourceVersion':obj['metadata']['resourceVersion']}}
        k.run('delete','--raw','/apis/'+group+'.k8s.elastic.co/v1/namespaces/logging/'+plural+'/logging','-f','-',body=json.dumps(options))
        k.run('-n','logging','wait','--for=delete',kind+'/logging','--timeout=10m',timeout=660)


def boundary_refusals(lab, record):
    """Wrong configurations must refuse before either product is changed."""
    original = lab.configuration()
    k = lab.api()
    before = {topology: lab.topology_state(topology) for topology in ('standard','compact')}
    fixtures = []
    try:
        c = lab.selected_config('standard')
        namespace = c['elasticsearch_namespace']
        lab.configuration(elasticsearch_eck_upgrade_from='3.4.1', elasticsearch_eck_upgrade_change_id='consumer-mistake')
        lab.product_refusal('business configuration cannot upgrade shared ECK','shared-upgrade')
        lab.configuration(elasticsearch_eck_upgrade_from='', elasticsearch_eck_upgrade_change_id='')
        store = json.loads((lab.STATE/'s3.json').read_text())
        req = lab.repository_request('standard',store)
        for path in ('elasticsearch','elasticsearch/logging','elasticsearch/logging/daily'):
            lab.configuration(elasticsearch_requests=[dict(req,id='reserved-backup',base_path=path)])
            lab.product_refusal('reserved EFK backup tree','backup-tree-'+path.replace('/','-'))
        # A second business cluster must not register the source's backup tree.
        # First qualify the source registration through the current product.
        lab.configuration(elasticsearch_requests=[req])
        lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
        lab.selected_config('compact')
        for number,path in enumerate((req['base_path'],store['owner'],req['base_path']+'/daily')):
            lab.configuration(elasticsearch_requests=[dict(req,id='cross-writer',name='isolation-overlap',base_path=path)])
            lab.product_refusal('overlaps another writer','cross-writer'+('-'+str(number) if number else ''))
        lab.selected_config('standard')
        lab.configuration(elasticsearch_requests=[])
        for target, source_ns, source_secret, component in (
                (namespace,'logging','logging-snapshot-s3','elasticsearch'),
                ('logging',namespace,c['elasticsearch_s3_secret'],'logging')):
            name = 'es-isolation-'+secrets.token_hex(6)
            source = k.get('secret',source_secret,source_ns)
            if not source: raise ValueError('credential isolation fixture requires the real source Secret')
            k.apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':target,'labels':lab.LABELS},
                      'type':'Opaque','data':source['data']}])
            fixture = k.get('secret',name,target)
            fixtures.append((target,name,fixture['metadata']['uid']))
            if component == 'elasticsearch':
                lab.configuration(elasticsearch_s3_secret=name)
                lab.product_refusal('distinct S3 access keys','business-credential-alias',marker='ECK_SHARED_REFUSED')
                lab.configuration(elasticsearch_s3_secret=c['elasticsearch_s3_secret'])
            else:
                saved = lab.configuration()['logging_efk_snapshot_secret']
                lab.configuration(logging_efk_snapshot_secret=name)
                lab.product_refusal('distinct S3 access keys','logging-credential-alias',tags='logging',marker='ECK_SHARED_REFUSED')
                lab.configuration(logging_efk_snapshot_secret=saved)
        lab.configuration(logging_namespace=namespace)
        lab.product_refusal('consumer namespace belongs to another product','logging-namespace',tags='logging',marker='ECK_SHARED_REFUSED')
        lab.configuration(logging_namespace='logging',logging_elasticsearch_ver='9.5.6',logging_kibana_ver='9.5.6')
        lab.product_refusal('EFK version is outside shared ECK qualification','logging-version',tags='logging',marker='ECK_SHARED_REFUSED')
    finally:
        primary = sys.exc_info()[1]
        cleanup_errors = []
        try:
            lab.configuration(**original)
        except Exception as exc:
            cleanup_errors.append(exc)
        for namespace,name,uid in fixtures:
            try:
                obj = k.get('secret',name,namespace)
                if (not obj or obj['metadata']['uid'] != uid or
                        any(obj['metadata'].get('labels',{}).get(key) != value for key,value in lab.LABELS.items())):
                    raise ValueError('isolation fixture Secret identity changed before cleanup')
                options = {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{
                    'uid':uid,'resourceVersion':obj['metadata']['resourceVersion']}}
                k.run('delete','--raw','/api/v1/namespaces/'+namespace+'/secrets/'+name,'-f','-',body=json.dumps(options))
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            print('ELASTICSEARCH_ISOLATION_CLEANUP_ERROR count='+str(len(cleanup_errors)), file=sys.stderr, flush=True)
            if primary is None:
                raise cleanup_errors[0]
    try:
        verify(lab,record)
        for topology in ('standard','compact'):
            lab.verify_topology(topology);lab.data_ledger(topology)
            if lab.topology_state(topology) != before[topology]:
                raise ValueError('refused isolation operation altered business identities')
    finally:
        lab.configuration(**original)
    print('ELASTICSEARCH_ISOLATION_REFUSALS_PASS platform_upgrade=refused backup_overlap=refused credentials=distinct namespace=protected',flush=True)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-53-credential-refusal',flush=True)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-54-efk-upgrade-refusal',flush=True)


def confirm_write(request):
    """Prove a new replicated business write after recovery, then remove only it."""
    name='es-eck-recovery-'+secrets.token_hex(8);document={'probe':name};identity=None
    if request('GET','/'+name+'/_settings?ignore_unavailable=true'):
        raise ValueError('ECK recovery write probe already exists')
    request('PUT','/'+name,{'settings':{'number_of_shards':1,'number_of_replicas':1}})
    identity=request('GET','/'+name+'/_settings')[name]['settings']['index']['uuid']
    try:
        result=request('PUT','/'+name+'/_create/probe?wait_for_active_shards=all',document)
        if result.get('_shards',{}).get('successful',0)<2 or result['_shards']['failed']:
            raise ValueError('ECK recovery write lacks replicated acknowledgement')
        request('POST','/'+name+'/_refresh')
        if request('GET','/'+name+'/_doc/probe')['_source']!=document or request('GET','/'+name+'/_count')['count']!=1:
            raise ValueError('ECK recovery write differs from its confirmed document')
    finally:
        current=request('GET','/'+name+'/_settings')[name]['settings']['index']['uuid']
        if not identity or current!=identity:raise ValueError('ECK recovery write cleanup refuses a changed index identity')
        request('DELETE','/'+name)


def shared_recovery(lab, record):
    k = lab.api()
    pod = k.get('pod','elastic-operator-0','elastic-system')
    if (not pod or not any(owner.get('uid') == record['identities']['operator_uid']
                          for owner in pod['metadata'].get('ownerReferences',[]))):
        raise ValueError('shared Operator fault refuses an unproven Pod owner')
    options = {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{
        'uid':pod['metadata']['uid'],'resourceVersion':pod['metadata']['resourceVersion']}}
    k.run('delete','--raw','/api/v1/namespaces/elastic-system/pods/elastic-operator-0','-f','-',body=json.dumps(options))
    deadline = time.monotonic()+180
    while time.monotonic()<deadline:
        current = k.get('pod','elastic-operator-0','elastic-system')
        if (current and current['metadata']['uid'] != pod['metadata']['uid'] and
                any(owner.get('uid') == record['identities']['operator_uid'] for owner in current['metadata'].get('ownerReferences',[])) and
                any(condition.get('type') == 'Ready' and condition.get('status') == 'True' for condition in current.get('status',{}).get('conditions',[]))):
            break
        print('ELASTICSEARCH_WAIT_HEARTBEAT coexistence=shared-eck-recovery',flush=True)
        time.sleep(5)
    else:
        raise ValueError('shared Operator Pod was not replaced and ready within 180 seconds')
    original = lab.configuration()
    try:
        for topology in ('standard','compact'):
            lab.selected_config(topology)
            lab.configuration(elasticsearch_requests=[])
            lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
            lab.verify_topology(topology);lab.data_ledger(topology)
            lab.client_job(topology,'business')
        configure(lab)
        lab.product('setup',lab.NAME,'07','--tags','logging',timeout=5400)
        verify(lab,record)
        with logging_connection(lab) as request:confirm_write(request)
    finally:
        lab.configuration(**original)
    print('ELASTICSEARCH_SHARED_ECK_RECOVERY_PASS efk=verified business=verified backups=preserved',flush=True)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-53-eck-recovery',flush=True)


def ownership_refusal(lab, record):
    """A foreign auxiliary object must never be adopted, even in an owned namespace."""
    original=lab.configuration();k=lab.api();namespace='es-isolation-owner'
    if k.get('namespace',namespace):raise ValueError('ownership probe namespace already exists')
    lab.scenario_namespace(namespace)
    uid=k.get('namespace',namespace)['metadata']['uid']
    try:
        c=lab.selected_config('compact')
        name=c['elasticsearch_name']+'-client'
        k.apply([{'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':namespace,
            'labels':dict(lab.LABELS,**{'kubeauto.io/component':'foreign-fixture'})},
            'spec':{'ports':[{'port':9200,'targetPort':9200}]}}])
        before=k.get('service',name,namespace)
        lab.configuration(elasticsearch_namespace=namespace,elasticsearch_requests=[],elasticsearch_s3_secret='',
            elasticsearch_http_tls_secret='',elasticsearch_admin_secret='',elasticsearch_file_realm_secret='',
            elasticsearch_s3_ca_configmap='',elasticsearch_exporter_enabled=False,elasticsearch_kibana_enabled=False)
        lab.product_refusal('foreign product resource: service/'+name,'foreign-resource')
        if k.get('service',name,namespace)!=before:
            raise ValueError('refused operation changed the foreign fixture')
        verify(lab,record)
        for topology in ('standard','compact'):lab.verify_topology(topology);lab.data_ledger(topology)
    finally:
        lab.configuration(**original)
        obj=k.get('namespace',namespace)
        if (not obj or obj['metadata']['uid']!=uid or
                any(obj['metadata'].get('labels',{}).get(key)!=value for key,value in lab.LABELS.items())):
            raise ValueError('ownership probe namespace identity changed before cleanup')
        options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid,'resourceVersion':obj['metadata']['resourceVersion']}}
        k.run('delete','--raw','/api/v1/namespaces/'+namespace,'-f','-',body=json.dumps(options))
        k.run('wait','--for=delete','namespace/'+namespace,'--timeout=120s',timeout=150)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-53-ownership-refusal',flush=True)


def repository_refusals(lab, record):
    """Inject a misleading native alias; all product writes/restores must recheck it."""
    import hashlib
    original=lab.configuration();k,es=lab.api(),lab.load_es()
    c=lab.selected_config('compact');store=json.loads((lab.STATE/'s3.json').read_text())
    token=secrets.token_hex(6);alias='isolation-untrusted-'+token
    settings={'bucket':store['bucket'],'base_path':'elasticsearch/logging',
              'endpoint':store['endpoint'],'region':'us-east-1','readonly':False}
    created=False;journals=[]
    try:
        with es.connection(c,k) as request:
            if alias in request('GET','/_snapshot/_all'):raise ValueError('repository refusal alias already exists')
            # verify=false creates only the owned alias; it does not read or
            # write the reserved tree. The product must reject the deception.
            request('PUT','/_snapshot/'+alias+'?verify=false',{'type':'s3','settings':settings})
            created=True
        for kind in ('snapshot','slm'):
            req={'id':'isolation-'+kind+'-'+token,'type':kind}
            if kind=='snapshot':req.update(repository=alias,snapshot='must-not-exist',indices=['must-not-exist'])
            else:req.update(name='must-not-exist',policy={'repository':alias,'schedule':'0 0 0 * * ?',
                'name':'<must-not-exist-{now/d}>','config':{'indices':['must-not-exist']},'retention':{'max_count':1}})
            lab.configuration(elasticsearch_requests=[req],elasticsearch_repository_role='writer')
            lab.product_refusal('reserved EFK backup tree','alias-'+kind)
        with es.connection(c,k) as request:
            settings['readonly']=True
            request('PUT','/_snapshot/'+alias+'?verify=false',{'type':'s3','settings':settings})
            cluster_uuid=request('GET','/')['cluster_uuid']
        for replay in (False,True):
            req={'id':'isolation-restore-'+token+('-replay' if replay else ''),'type':'restore',
                 'repository':alias,'snapshot':'must-not-exist','indices':['must-not-exist']}
            if replay:
                name=c['elasticsearch_name']+'-request-'+req['id']
                if k.get('configmap',name,c['elasticsearch_namespace']):raise ValueError('restore replay fixture already exists')
                k.apply([{'apiVersion':'v1','kind':'ConfigMap','metadata':es.meta(c,name),'data':{
                    'cluster_uuid':cluster_uuid,'phase':'complete','receipt':'{}',
                    'fingerprint':hashlib.sha256(json.dumps(req,sort_keys=True,separators=(',',':')).encode()).hexdigest()}}])
                journals.append((name,k.get('configmap',name,c['elasticsearch_namespace'])['metadata']['uid']))
            lab.configuration(elasticsearch_requests=[req],elasticsearch_repository_role='reader')
            lab.product_refusal('reserved EFK backup tree','alias-restore-'+str(replay).lower())
        verify(lab,record)
        for topology in ('standard','compact'):lab.verify_topology(topology);lab.data_ledger(topology)
    finally:
        lab.configuration(**original)
        if created:
            with es.connection(c,k) as request:
                current=request('GET','/_snapshot/'+alias)[alias]
                if current['type']!='s3' or any(str(current['settings'].get(key)).lower()!=str(value).lower() for key,value in settings.items()):
                    raise ValueError('repository refusal alias identity changed before cleanup')
                request('DELETE','/_snapshot/'+alias)
        for name,uid in journals:
            obj=k.get('configmap',name,c['elasticsearch_namespace'])
            if not obj or obj['metadata']['uid']!=uid or not es.owned_resource(c,obj):
                raise ValueError('restore replay journal identity changed before cleanup')
            options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid,'resourceVersion':obj['metadata']['resourceVersion']}}
            k.run('delete','--raw','/api/v1/namespaces/'+c['elasticsearch_namespace']+'/configmaps/'+name,'-f','-',body=json.dumps(options))
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-53-repository-refusal',flush=True)


def restore_probe(request, index, document, expected_uuid=None):
    settings=request('GET','/'+index+'/_settings?ignore_unavailable=true')
    if not settings:return None
    current=settings[index]['settings']['index']['uuid']
    if (expected_uuid and current!=expected_uuid or
            request('GET','/'+index+'/_doc/sentinel')['_source']!=document or
            request('GET','/'+index+'/_count')['count']!=1):
        raise ValueError('coexistence recovery refuses a changed probe')
    return current


def isolated_restores(lab, record, resume=False):
    """Restore owned probes while the other product remains live and verified."""
    original = lab.configuration()
    k, es = lab.api(), lab.load_es()
    path=lab.STATE/'coexistence-restores.json'
    saved=json.loads(path.read_text()) if path.exists() else None
    if saved and saved['logging']!=record:
        raise ValueError('coexistence recovery journal belongs to another fixture')
    token = saved['token'] if saved else secrets.token_hex(8)
    if resume and not saved:
        # The previous runner froze this exact product input before the analysis
        # timeout. Adopt only that owned source probe, after checking its data.
        rendered=json.loads((lab.DIRECTORY/'yml/elasticsearch/input.json').read_text())
        requests=rendered.get('elasticsearch_requests',[])
        store=json.loads((lab.STATE/'s3.json').read_text())
        if (len(requests)!=2 or requests[0]!=lab.repository_request('standard',store) or
                requests[1].get('type')!='snapshot' or
                not re.fullmatch(r'isolation-[0-9a-f]{16}',requests[1].get('snapshot',''))):
            raise ValueError('coexistence recovery requires the frozen failed snapshot input')
        token=requests[1]['snapshot'].removeprefix('isolation-')
        if requests[1]!={'id':'isolation-'+token,'type':'snapshot','repository':requests[0]['name'],
                        'snapshot':'isolation-'+token,'indices':['es-isolation-'+token]}:
            raise ValueError('coexistence recovery refuses an unrelated frozen probe')
    index = 'es-isolation-'+token
    snapshot = 'isolation-'+token
    document = {'token':token, 'purpose':'coexistence recovery'}
    identities_before = saved['identities'] if saved else {name:lab.topology_state(name) for name in ('standard','compact')}
    store = json.loads((lab.STATE/'s3.json').read_text())
    journal=saved or {'logging':record,'token':token,'identities':identities_before,'uuids':{},'completed':[]}
    steps=('snapshot','business','logging','cleanup')
    if journal['completed']!=list(steps[:len(journal['completed'])]) or len(journal['completed'])>len(steps):
        raise ValueError('coexistence recovery journal has an invalid stage order')
    def complete(step):
        if step not in journal['completed']:journal['completed'].append(step)
        lab.atomic(path,journal)
    try:
        c = lab.selected_config('standard')
        if not journal['completed']:
            with es.connection(c,k) as request:
                if saved or resume:
                    uid=restore_probe(request,index,document,journal['uuids'].get('standard'))
                    if not uid:raise ValueError('coexistence recovery lost its retained source probe')
                else:
                    if request('GET','/'+index+'/_settings?ignore_unavailable=true'):
                        raise ValueError('coexistence recovery probe already exists')
                    request('PUT','/'+index,{'settings':{'number_of_shards':1,'number_of_replicas':1}})
                    result = request('PUT','/'+index+'/_create/sentinel?wait_for_active_shards=all',document)
                    if result.get('_shards',{}).get('successful',0)<2 or result['_shards']['failed']:
                        raise ValueError('coexistence recovery probe lacks replicated acknowledgement')
                    request('POST','/'+index+'/_refresh')
                    uid=restore_probe(request,index,document)
                journal['uuids']['standard']=uid
                lab.atomic(path,journal)
        repository = lab.repository_request('standard',store)
        if 'snapshot' not in journal['completed']:
            lab.configuration(elasticsearch_requests=[repository,{'id':snapshot,'type':'snapshot',
                'repository':repository['name'],'snapshot':snapshot,'indices':[index]}])
            lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
            complete('snapshot')
        verify(lab,record)
        c = lab.selected_config('compact')
        if 'business' not in journal['completed']:
            lab.configuration(elasticsearch_repository_role='reader',elasticsearch_requests=[dict(repository,
                id='reader-'+token,name='isolation-reader'),{'id':'restore-'+token,'type':'restore',
                'repository':'isolation-reader','snapshot':snapshot,'indices':[index]}])
            lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
            c = lab.configuration()
            with es.connection(c,k) as request:
                request('POST','/'+index+'/_refresh')
                uid=restore_probe(request,index,document,journal['uuids'].get('compact'))
                if not uid:raise ValueError('business coexistence restore lost its probe')
                journal['uuids']['compact']=uid
                complete('business')
        verify(lab,record)
        # EFK's delivered recovery interface is its authenticated native API.
        # Rename its recorded sentinel into an unused index; never overwrite
        # either live product's original data to manufacture recovery evidence.
        restored = record['index']+'-recovered'
        if 'logging' not in journal['completed']:
            with logging_connection(lab) as request:
                if request('GET','/'+restored+'/_settings?ignore_unavailable=true'):
                    raise ValueError('EFK recovery target already exists')
                response = request('POST','/_snapshot/logging-s3/'+record.get('snapshot','coexistence')+'/_restore?wait_for_completion=true',
                    {'indices':record['index'],'include_global_state':False,'feature_states':['none'],
                     'rename_pattern':'(.+)','rename_replacement':'$1-recovered'})
                shards = response.get('snapshot',{}).get('shards',{})
                if shards.get('failed') != 0 or not shards.get('successful'):
                    raise ValueError('EFK coexistence restore lacks completed shard recovery')
                request('POST','/'+restored+'/_refresh')
                if (request('GET','/'+restored+'/_doc/sentinel')['_source'] != record['document'] or
                        request('GET','/'+restored+'/_count')['count'] != 1):
                    raise ValueError('EFK coexistence restore differs from its independent snapshot')
                # Only this function's verified, newly restored probe is removed.
                request('DELETE','/'+restored)
            complete('logging')
        for topology in ('standard','compact'):
            c = lab.selected_config(topology)
            if 'cleanup' not in journal['completed']:
                with es.connection(c,k) as request:
                    if restore_probe(request,index,document,journal['uuids'][topology]):
                        request('DELETE','/'+index)
                    if topology == 'compact':request('DELETE','/_snapshot/isolation-reader')
                    else:request('DELETE','/_snapshot/'+repository['name']+'/'+snapshot)
            lab.verify_topology(topology);lab.data_ledger(topology)
            if lab.topology_state(topology) != identities_before[topology]:
                raise ValueError('single-product restore altered existing business identities')
        verify(lab,record)
        complete('cleanup')
    finally:
        lab.configuration(**original)
    print('ELASTICSEARCH_COEXISTENCE_RESTORES_PASS efk=restored business=restored peer_data=verified',flush=True)
    for case in ('ES-54-business-restore','ES-54-efk-restore'):
        print('ELASTICSEARCH_SUBCASE_PASS id='+case,flush=True)


def upgrade_storage(lab):
    """Budget three small probe volumes without inheriting expansion fixtures."""
    size=lab.load_es().quantity('2Gi')
    slots=[]
    for node in lab.api().get('nodes.longhorn.io',namespace='longhorn-system')['items']:
        if node['metadata']['name'] not in lab.NODES or not node['spec'].get('allowScheduling',True):continue
        count=0
        for name,disk in node.get('status',{}).get('diskStatus',{}).items():
            spec=node['spec']['disks'].get(name,{})
            if not spec.get('allowScheduling',True):continue
            if any(condition['status']!='True' for condition in disk.get('conditions',[]) if condition['type']=='Ready'):continue
            capacity=min(disk['storageMaximum']-spec.get('storageReserved',0)-disk['storageScheduled'],
                         disk['storageAvailable']-disk['storageMaximum']//4)
            count+=max(0,capacity//size)
        slots.append(count)
    for _ in range(3):
        slots.sort(reverse=True)
        if len(slots)<3 or slots[2]<1:
            raise ValueError('coexistence upgrade storage cannot admit three replicated 2Gi probe volumes')
        for i in range(3):slots[i]-=1
    print('ELASTICSEARCH_COEXISTENCE_UPGRADE_STORAGE_PASS volumes=3 size=2Gi replicas=3',flush=True)


def remove_upgrade_namespace(lab, namespace_uid):
    namespace='es-isolation-upgrade';k=lab.api()
    obj=k.get('namespace',namespace)
    if (not obj or obj['metadata']['uid']!=namespace_uid or
            any(obj['metadata'].get('labels',{}).get(key)!=value for key,value in lab.LABELS.items())):
        raise ValueError('coexistence upgrade cleanup refuses a changed namespace')
    options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{
        'uid':namespace_uid,'resourceVersion':obj['metadata']['resourceVersion']}}
    k.run('delete','--raw','/api/v1/namespaces/'+namespace,'-f','-',body=json.dumps(options))
    k.run('wait','--for=delete','namespace/'+namespace,'--timeout=10m',timeout=660)
    path=lab.STATE/'namespaces.json'
    owners=json.loads(path.read_text());owners.pop(namespace,None);lab.atomic(path,owners)


def cleanup_retained_upgrade(lab):
    """Retire only the failed runner's exclusively owned upgrade namespace."""
    namespace='es-isolation-upgrade';obj=lab.api().get('namespace',namespace)
    if not obj:return
    owners=json.loads((lab.STATE/'namespaces.json').read_text())
    frozen=json.loads((lab.DIRECTORY/'yml/elasticsearch/input.json').read_text())
    if (owners.get(namespace)!=obj['metadata']['uid'] or
            any(obj['metadata'].get('labels',{}).get(key)!=value for key,value in lab.LABELS.items()) or
            frozen.get('elasticsearch_namespace')!=namespace or frozen.get('elasticsearch_ver') not in ('9.5.5','9.5.6')):
        raise ValueError('coexistence upgrade refuses an unbound retained fixture')
    lab.configuration(**dict(frozen,elasticsearch_state='absent',elasticsearch_requests=[]))
    lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
    remove_upgrade_namespace(lab,obj['metadata']['uid'])
    print('ELASTICSEARCH_COEXISTENCE_UPGRADE_REPAIR_CLEAN_PASS scope=owned-upgrade-only',flush=True)


def business_upgrade(lab, record):
    """Qualify the supported Stack upgrade beside EFK without downgrading peers."""
    original = lab.configuration()
    k, es = lab.api(), lab.load_es()
    namespace = 'es-isolation-upgrade'
    before = {name:lab.topology_state(name) for name in ('standard','compact')}
    try:
        cleanup_retained_upgrade(lab)
        upgrade_storage(lab)
        c = lab.selected_config('compact')
        planning = json.loads((lab.STATE/'coexistence-capacity.json').read_text())
        lab.scenario_namespace(namespace)
        namespace_uid = k.get('namespace',namespace)['metadata']['uid']
        c = lab.configuration(elasticsearch_namespace=namespace,elasticsearch_ver='9.5.5',
            elasticsearch_data_nodes=planning['upgrade_nodes'],elasticsearch_data_count=3,elasticsearch_data_pvc_size='2Gi',
            elasticsearch_smartcn_enabled=False,elasticsearch_kibana_enabled=False,
            elasticsearch_exporter_enabled=False,elasticsearch_s3_secret='',elasticsearch_s3_ca_configmap='',
            elasticsearch_http_tls_secret='',elasticsearch_admin_secret='',elasticsearch_file_realm_secret='',
            elasticsearch_requests=[])
        lab.product('download','-E','elasticsearch-qualification',timeout=5400)
        lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
        previous = lab.topology_state('compact',config=c)
        token = secrets.token_hex(8)
        index = 'es-isolation-upgrade-'+token
        document = {'token':token}
        with es.connection(c,k) as request:
            request('PUT','/'+index,{'settings':{'number_of_shards':1,'number_of_replicas':1}})
            result = request('PUT','/'+index+'/_create/sentinel?wait_for_active_shards=all',document)
            if result.get('_shards',{}).get('successful',0)<2 or result['_shards']['failed']:
                raise ValueError('coexistence upgrade probe lacks replicated acknowledgement')
        verify(lab,record)
        c = lab.configuration(elasticsearch_ver='9.5.6')
        lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
        with es.connection(c,k) as request:
            if request('GET','/')['version']['number'] != '9.5.6':
                raise ValueError('coexistence Stack upgrade did not reach the target version')
            request('POST','/'+index+'/_refresh')
            if request('GET','/'+index+'/_doc/sentinel')['_source'] != document:
                raise ValueError('coexistence Stack upgrade changed confirmed data')
        after = lab.topology_state('compact',config=c)
        for key in ('cr_uid','pvc_uids','credentials_uid','credentials_sha256'):
            if previous[key] != after[key]:
                raise ValueError('coexistence Stack upgrade changed persistent consumer identities')
        verify(lab,record)
        for topology in ('standard','compact'):
            lab.verify_topology(topology);lab.data_ledger(topology)
            if lab.topology_state(topology) != before[topology]:
                raise ValueError('single-consumer upgrade altered a peer identity')
        lab.configuration(**dict(c,elasticsearch_state='absent',elasticsearch_requests=[]))
        lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
        verify(lab,record)
        remove_upgrade_namespace(lab,namespace_uid)
    finally:
        lab.configuration(**original)
    print('ELASTICSEARCH_COEXISTENCE_UPGRADE_PASS from=9.5.5 to=9.5.6 efk=verified peers=verified',flush=True)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-54-business-upgrade',flush=True)


def run(lab):
    lab.baseline();k=lab.api()
    original=lab.configuration()
    if not k.get('namespace','monitor') or not k.get('storageclass','local-path'):
        raise ValueError('coexistence requires the product monitoring/local-path baseline')
    if (lab.STATE/'coexistence.json').exists():return resume(lab,original)
    retained=k.get('namespace','logging')
    if retained:
        owners=json.loads((lab.STATE/'namespaces.json').read_text())
        if (retained['metadata']['uid']!=owners.get('logging') or
                any(retained['metadata'].get('labels',{}).get(key)!=value for key,value in lab.LABELS.items()) or
                not (lab.STATE/'coexistence-capacity.json').is_file()):
            raise ValueError('coexistence preparation refuses an unbound retained fixture')
    else:
        capacity(lab)
        fixtures(lab)
    # Admit only the three data candidates budgeted away from EFK. An extra
    # unused candidate must not accidentally consume EFK's reserved capacity
    # during later business reconciliation. This is owned fixture configuration.
    plan=json.loads((lab.STATE/'coexistence-capacity.json').read_text())
    groups=[plan.get(key) for key in ('logging_nodes','upgrade_nodes')]
    if (any(not isinstance(nodes,list) or len(nodes)!=3 or len(set(nodes))!=3 or
            not set(nodes)<=set(lab.NODES) for nodes in groups) or set(groups[0])&set(groups[1])):
        raise ValueError('coexistence preparation refuses an invalid capacity plan')
    try:
        c=lab.selected_config('compact')
        if c['elasticsearch_data_nodes']!=plan['upgrade_nodes']:
            lab.selected_config('compact',elasticsearch_data_nodes=plan['upgrade_nodes'])
            lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
            lab.verify_topology('compact');lab.data_ledger('compact')
        namespace_uid=k.get('namespace','logging')['metadata']['uid']
        before={topology:lab.topology_state(topology) for topology in ('standard','compact')}
        operator_uid=k.get('statefulset','elastic-operator','elastic-system')['metadata']['uid']
        # The leased source already has qualified CSI and live business PVCs.
        # Scope these addon calls to consumers; finally restores the original
        # storage configuration on success and failure.
        configure(lab)
        lab.product('download','-E','logging-efk',timeout=5400);lab.product('download','-E','ingress-nginx',timeout=5400)
        lab.product('setup',lab.NAME,'07',timeout=5400)
        lab.bind_namespace('ingress-nginx')
        seed_path=lab.STATE/'coexistence-seed.json'
        if seed_path.exists():
            seed=json.loads(seed_path.read_text())
            if seed['namespace_uid']!=namespace_uid:raise ValueError('coexistence seed namespace changed')
        else:
            token=secrets.token_hex(8)
            seed={'namespace_uid':namespace_uid,'index':'es-gate-coexistence-'+token,
                  'document':{'token':token,'purpose':'isolated logging business'},'snapshot':'coexistence-'+token}
            lab.atomic(seed_path,seed)
        index,document=seed['index'],seed['document']
        with logging_connection(lab) as request:
            cluster_uuid=request('GET','/')['cluster_uuid']
            if request('GET','/')['version']['number']!='9.5.1':raise ValueError('business installation changed the EFK pinned version')
            if not request('GET','/'+index+'/_settings?ignore_unavailable=true'):
                request('PUT','/'+index,{'settings':{'number_of_shards':3,'number_of_replicas':1}})
            prior=request('POST','/'+index+'/_mget',{'ids':['sentinel']})['docs']
            if len(prior)!=1 or prior[0].get('found') and prior[0].get('_source')!=document:
                raise ValueError('coexistence seed data changed; refuse overwrite')
            result=request('PUT','/'+index+'/_doc/sentinel?wait_for_active_shards=all&refresh=wait_for',document)
            if result.get('_shards',{}).get('successful',0)<2 or result['_shards']['failed']:raise ValueError('EFK sentinel lacks replicated acknowledgement')
            snapshots=request('GET','/_snapshot/logging-s3/'+seed['snapshot']+'?ignore_unavailable=true')['snapshots']
            snapshot=snapshots[0] if snapshots else request('PUT','/_snapshot/logging-s3/'+seed['snapshot']+'?wait_for_completion=true',
                {'indices':index,'include_global_state':False,'feature_states':['none']})['snapshot']
            if (snapshot['state']!='SUCCESS' or snapshot['shards']['failed'] or
                    snapshot.get('indices')!=[index] or not snapshot['shards'].get('total')):
                raise ValueError('EFK sentinel backup failed or differs from the owned seed')
        record={'identities':identities(lab),'cluster_uuid':cluster_uuid,'index':index,'document':document,
                'snapshot':seed['snapshot'],'snapshot_uuid':snapshot['uuid']}
        lab.atomic(lab.STATE/'coexistence.json',record)
        if record['identities']['operator_uid']!=operator_uid:raise ValueError('EFK created or replaced the shared ECK')
        for topology in ('standard','compact'):
            lab.verify_topology(topology);lab.data_ledger(topology)
            if lab.topology_state(topology)!=before[topology]:raise ValueError('EFK installation altered business topology or credentials')
        print('ELASTICSEARCH_SUBCASE_PASS id=ES-53-es-then-efk',flush=True)
        boundary_refusals(lab,record)
        ownership_refusal(lab,record)
        repository_refusals(lab,record)
        shared_recovery(lab,record)
        isolated_restores(lab,record)
        business_upgrade(lab,record)
        business_reentry(lab,record)
        reverse(lab,record)
        for number in (53,54):print('ELASTICSEARCH_CASE_PASS id=ES-'+str(number),flush=True)
    finally:
        # Preserve the first failure and its live EFK state for diagnostics.
        # Full cleanup owns this recorded namespace on either outcome.
        lab.configuration(**dict(original,logging_install='no'))
    finish(lab,namespace_uid)


def business_reentry(lab, record):
    k=lab.api()
    for topology in ('standard','compact'):
        lab.verify_topology(topology);lab.data_ledger(topology)
        before=lab.topology_state(topology);c=lab.selected_config(topology)
        customer={name:k.get('secret',name,c['elasticsearch_namespace']) for name in (c['elasticsearch_s3_secret'],) if name}
        lab.selected_config(topology,elasticsearch_state='absent',elasticsearch_requests=[])
        lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
        claims={p['metadata']['name']:p['metadata']['uid'] for p in k.get('pvc',namespace=c['elasticsearch_namespace'])['items']}
        if claims!=before['pvc_uids']:raise ValueError('business uninstall removed ECK claims')
        for name,saved in customer.items():
            current=k.get('secret',name,c['elasticsearch_namespace'])
            if not current or current['metadata']['uid']!=saved['metadata']['uid'] or current['data']!=saved['data']:
                raise ValueError('business uninstall removed customer S3 credentials')
        verify(lab,record)
        lab.selected_config(topology,elasticsearch_state='present')
        lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
        lab.verify_topology(topology);lab.data_ledger(topology);verify(lab,record)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-54-business-uninstall',flush=True)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-53-efk-then-es',flush=True)


def restores_repair(lab):
    isolation_repair(lab,restores_only=True)


def isolation_repair(lab, restores_only=False):
    """Qualify the remaining isolation branch; never re-sign a failed parent."""
    lab.baseline();k=lab.api();original=lab.configuration()
    record=json.loads((lab.STATE/'coexistence.json').read_text())
    owners=json.loads((lab.STATE/'namespaces.json').read_text());namespace=k.get('namespace','logging')
    if (not namespace or namespace['metadata']['uid']!=owners.get('logging') or
            any(namespace['metadata'].get('labels',{}).get(key)!=value for key,value in lab.LABELS.items()) or
            list(lab.STATE.glob('fault-*.json'))):
        raise ValueError('isolation repair requires a bound fault-free coexistence fixture')
    path=lab.STATE/'coexistence-isolation-progress.json'
    steps=('isolated_restores','business_upgrade','business_reentry','reverse')
    progress=json.loads(path.read_text()) if restores_only and path.exists() else {'logging':record,'completed':[]}
    done=progress['completed']
    if progress['logging']!=record or done!=list(steps[:len(done)]) or len(done)>len(steps):
        raise ValueError('isolation repair refuses a foreign or unordered continuation')
    if (restores_only and done==list(steps[:3]) and
            not any(k.get(kind,'logging','logging') for kind in ('elasticsearch','kibana'))):
        # The previous reverse branch already retired both owned consumers.
        # Its established recovery path validates retained identities/snapshot
        # before reentry; an absent API cannot pass the normal live verifier.
        resume(lab,original)
        done.append('reverse');lab.atomic(path,progress)
        print('ELASTICSEARCH_COEXISTENCE_CONTINUATION stage=reverse completed=4/4',flush=True)
        print('ELASTICSEARCH_COEXISTENCE_ISOLATION_REPAIR_PASS cases_accepted=0 prior_failure=preserved',flush=True)
        return
    try:
        verify(lab,record)
        for topology in ('standard','compact'):lab.verify_topology(topology);lab.data_ledger(topology)
        if not restores_only:
            repository_refusals(lab,record)
            shared_recovery(lab,record)
            isolated_restores(lab,record)
            business_upgrade(lab,record)
            business_reentry(lab,record)
            reverse(lab,record)
        else:
            for step in steps:
                if step in done:continue
                if step=='isolated_restores':isolated_restores(lab,record,resume=True)
                else:globals()[step](lab,record)
                done.append(step);lab.atomic(path,progress)
                print('ELASTICSEARCH_COEXISTENCE_CONTINUATION stage='+step+' completed='+str(len(done))+'/4',flush=True)
    finally:
        lab.configuration(**dict(original,logging_install='no'))
    finish(lab,namespace['metadata']['uid'])
    print('ELASTICSEARCH_COEXISTENCE_ISOLATION_REPAIR_PASS cases_accepted=0 prior_failure=preserved',flush=True)


def reverse(lab, record):
    lab.configuration(logging_install='no')
    unload_efk(lab)
    if identities(lab)!=record['identities']:raise ValueError('EFK uninstall removed claims/customer credentials/shared ECK')
    minio_job(lab,'mc stat --recursive gate/logging-snapshots/elasticsearch/logging >/dev/null','snapshot-retained-after-uninstall')
    for topology in ('standard','compact'):lab.verify_topology(topology);lab.data_ledger(topology)
    configure(lab);lab.product('setup',lab.NAME,'07',timeout=5400)
    verify(lab,record)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-54-efk-uninstall',flush=True)


def finish(lab, namespace_uid):
    k=lab.api()
    if k.get('namespace','logging')['metadata']['uid']!=namespace_uid:raise ValueError('coexistence cleanup refuses a replaced namespace')
    unload_efk(lab)
    k.run('delete','namespace','logging','--wait=true','--timeout=10m',timeout=660)
    for topology in ('standard','compact'):lab.verify_topology(topology);lab.data_ledger(topology)
    print('ELASTICSEARCH_COEXISTENCE_SCOPED_CLEAN_PASS shared_eck=preserved customer_data=verified logging_fixture=removed',flush=True)


def resume(lab, original):
    """Qualify only the failed reverse boundary; do not sign the failed parent."""
    k=lab.api()
    record=json.loads((lab.STATE/'coexistence.json').read_text())
    owners=json.loads((lab.STATE/'namespaces.json').read_text())
    namespace=k.get('namespace','logging')
    if (not namespace or namespace['metadata']['uid']!=owners.get('logging') or
            any(namespace['metadata'].get('labels',{}).get(key)!=value for key,value in lab.LABELS.items())):
        raise ValueError('coexistence repair refuses an unbound or replaced namespace')
    if identities(lab)!=record['identities']:raise ValueError('coexistence repair retained identities changed')
    consumers=[k.get(kind,'logging','logging') for kind in ('elasticsearch','kibana')]
    retired=not any(consumers)
    if any(consumers) and not all(consumers):raise ValueError('coexistence repair refuses partial retirement')
    if retired:minio_job(lab,'mc stat --recursive gate/logging-snapshots/elasticsearch/logging >/dev/null','retained-snapshot-before-reentry')
    else:verify(lab,record)
    for topology in ('standard','compact'):lab.verify_topology(topology);lab.data_ledger(topology)
    try:
        configure(lab)
        if retired:
            lab.product('setup',lab.NAME,'07',timeout=5400)
            verify(lab,record)
            print('ELASTICSEARCH_SUBCASE_PASS id=ES-54-efk-uninstall',flush=True)
        else:reverse(lab,record)
    finally:
        lab.configuration(**dict(original,logging_install='no'))
    finish(lab,namespace['metadata']['uid'])
    print('ELASTICSEARCH_COEXISTENCE_REPAIR_PASS cases_accepted=0 reverse=verified prior_failure=preserved',flush=True)


def configure(lab):
    plan = lab.STATE/'coexistence-capacity.json'
    nodes = json.loads(plan.read_text())['logging_nodes'] if plan.exists() else list(lab.NODES[:3])
    lab.configuration(longhorn_install='no',elasticsearch_install='no',logging_install='yes',logging_solution='efk',logging_efk_delivery='direct',logging_namespace='logging',
        logging_efk_es_nodes=nodes,logging_efk_storage_class='local-path',logging_efk_es_pvc_size='4Gi',
        logging_efk_kibana_host='kibana.logging.test',logging_ingress_controller='ingress-nginx',logging_ingress_class='nginx',
        logging_ingress_tls_secret='logging-kibana-ingress-tls',logging_efk_snapshot_endpoint='http://minio.logging.svc:9000',
        logging_efk_snapshot_region='us-east-1',logging_efk_snapshot_bucket='logging-snapshots',logging_efk_snapshot_path_style=True,
        logging_efk_snapshot_secret='logging-snapshot-s3',logging_efk_writer_secret='logging-efk-writer',ingress_nginx_install='yes')
