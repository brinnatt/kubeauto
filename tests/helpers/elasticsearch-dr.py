#!/usr/bin/env python3
"""Independent, leased ES restore target; never consume the source cluster PVCs."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import time


BASE = Path('/usr/local/kubeauto')
SOURCE = Path('/var/tmp/kubeauto-elasticsearch')
HOSTS = ('192.168.122.135', '192.168.122.40', '192.168.122.72')
NODES = tuple('es-dr-'+address.rsplit('.', 1)[1] for address in HOSTS)


def target_lab():
    spec = importlib.util.spec_from_file_location('elasticsearch_target_lab', BASE/'tests/helpers/elasticsearch-lab.py')
    lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
    lab.HOSTS, lab.NODES, lab.NAME = HOSTS, NODES, 'elasticsearch-dr'
    lab.STATE, lab.DIRECTORY = SOURCE/'dr', BASE/'clusters/elasticsearch-dr'
    # The backend belongs to the source/control lease. Target cleanup must
    # never invoke the source backend cleanup helper with its default STATE.
    def backend(action):
        if action != 'cleanup': raise ValueError('DR target cannot own or mutate the external snapshot backend')
    lab.s3_fixture = backend
    return lab


def admission(lab):
    if lab.DIRECTORY.exists() or (lab.STATE/'ownership.json').exists():
        raise ValueError('DR admission refuses retained state; reuse its identity or run scoped cleanup')
    lab.execute(['bash', BASE/'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', *HOSTS], capture=False, timeout=900)
    hardware = {address: lab.host_capacity(address) for address in HOSTS}
    source = json.loads((SOURCE/'ownership.json').read_text())
    ids = {row['product_uuid'] for row in hardware.values()}
    source_ids = {row['product_uuid'] for row in source['hardware'].values()}
    if len(ids) != 3 or ids & source_ids or set(HOSTS) & set(source['hosts']):
        raise ValueError('DR target overlaps source host or VM identity')
    if any(row['free'] < 64*1024**3 for row in hardware.values()):
        raise ValueError('DR target requires measured root headroom for its owned loop filesystem')
    print('ELASTICSEARCH_DR_ADMISSION_PASS nodes=3 raw_disks=excluded source=independent', flush=True)
    return hardware


def bootstrap(lab):
    import uuid
    hardware = admission(lab)
    source_owner = json.loads((SOURCE/'ownership.json').read_text())['id']
    lab.atomic(lab.STATE/'ownership.json', {'hosts': list(HOSTS), 'hardware': hardware, 'id': str(uuid.uuid4()), 'source_owner': source_owner})
    # Admission already proved empty hosts; no destructive wipe is needed to
    # initialize the new independent Kubernetes cluster.
    lab.product('new', lab.NAME)
    inventory = []
    for group in ('etcd', 'kube_master', 'kube_node'):
        inventory += ['['+group+']']
        if group != 'kube_node':
            inventory += [address+" k8s_nodename='"+node+"'" for address, node in zip(HOSTS, NODES)]
    inventory += ['[all:vars]', 'SECURE_PORT="6443"', 'CONTAINER_RUNTIME="containerd"', 'CLUSTER_NETWORK="calico"', 'PROXY_MODE="ipvs"',
        'SERVICE_CIDR="10.84.0.0/16"', 'CLUSTER_CIDR="172.31.0.0/16"', 'NODE_PORT_RANGE="30000-32767"',
        'CLUSTER_DNS_DOMAIN="cluster.local"', 'bin_dir="/usr/local/bin"', 'base_dir="/usr/local/kubeauto"',
        'cluster_dir="{{ base_dir }}/clusters/elasticsearch-dr"', 'ca_dir="/etc/kubernetes/ssl"', 'ansible_user=root', "k8s_nodename=''", '']
    (lab.DIRECTORY/'hosts').write_text('\n'.join(inventory))
    lab.configuration(K8S_VER='1.33.6', KUBE_RESERVED_ENABLED='no', SYS_RESERVED_ENABLED='no', SYS_RESERVED_ENFORCE='no',
                      REGISTRY_HOST_IP='192.168.122.2', local_path_provisioner_install='no')
    lab.product('download', '-D', timeout=5400)
    lab.product('setup', lab.NAME, '90', timeout=7200)
    k = lab.api()
    source_uid = json.loads((SOURCE/'baseline.json').read_text())['cluster_uid']
    target_uid = k.get('namespace', 'kube-system')['metadata']['uid']
    if source_uid == target_uid: raise ValueError('DR target reused the source Kubernetes identity')
    lab.atomic(lab.STATE/'baseline.json', {'cluster_uid': target_uid, 'hosts': list(HOSTS), 'source_cluster_uid': source_uid})
    lab.baseline()
    k.run('uncordon', *NODES)
    lab.storage()
    print('ELASTICSEARCH_DR_PLATFORM_PASS independent_kubernetes=true', flush=True)


def baseline(lab):
    owner = json.loads((lab.STATE/'ownership.json').read_text())
    source = json.loads((SOURCE/'ownership.json').read_text())
    if owner['source_owner'] != source['id']: raise ValueError('DR target belongs to a different source lease')
    lab.baseline()


def install(lab, topology, *, version='9.5.6', features=False, upgrade_fixture=False):
    baseline(lab)
    k, es = lab.api(), lab.load_es()
    ns = 'es-'+topology
    for previous in ('standard', 'compact'):
        previous_ns = 'es-'+previous
        if previous_ns != ns and k.get('namespace', previous_ns):
            retire_business(lab, previous)
    lab.scenario_namespace(ns)
    source_config = json.loads((SOURCE/('config-'+topology+'.json')).read_text())
    source_keys = json.loads((SOURCE/'s3.json').read_text())
    ca = (SOURCE/'s3/certs/public.crt').read_text()
    for kind,name in (('secret','es-gate-s3'),('configmap','es-gate-s3-ca')):
        existing = k.get(kind,name,ns)
        if existing and not all(existing['metadata'].get('labels',{}).get(a)==b for a,b in lab.LABELS.items()):
            raise ValueError('DR restore refuses a foreign repository identity')
    k.apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':'es-gate-s3','namespace':ns,'labels':lab.LABELS},'type':'Opaque',
        'stringData':{'s3.client.default.access_key':source_keys['access_key'],'s3.client.default.secret_key':source_keys['secret_key']}},
        {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'es-gate-s3-ca','namespace':ns,'labels':lab.LABELS},'data':{'ca.crt':ca}}])
    c = lab.configuration(**dict(source_config, elasticsearch_ver=version, elasticsearch_topology='compact', elasticsearch_data_nodes=list(NODES),
        elasticsearch_data_count=3, elasticsearch_master_nodes=[], elasticsearch_namespace=ns, elasticsearch_state='present',
        elasticsearch_data_cpu='1', elasticsearch_data_memory='4Gi', elasticsearch_data_heap='2g', elasticsearch_data_pvc_size='4Gi',
        elasticsearch_kibana_enabled=False, elasticsearch_exporter_enabled=False, elasticsearch_http_tls_secret='',
        elasticsearch_admin_secret='', elasticsearch_file_realm_secret='', elasticsearch_smartcn_enabled=False,
        elasticsearch_eck_upgrade_from='', elasticsearch_eck_upgrade_change_id='', elasticsearch_requests=[],
        elasticsearch_repository_role='reader', elasticsearch_s3_secret='es-gate-s3', elasticsearch_s3_ca_configmap='es-gate-s3-ca'))
    if features:
        file_realm(lab, c)
        c = lab.configuration(elasticsearch_file_realm_secret='es-dr-file-realm', elasticsearch_admin_secret='es-dr-admin')
    if upgrade_fixture:c=lab.configuration(elasticsearch_kibana_enabled=True)
    lab.sysctl_fixture('snapshot')
    lab.product('download', '-E', 'elasticsearch', timeout=5400)
    if version != '9.5.6': lab.product('download', '-E', 'elasticsearch-qualification', timeout=5400)
    if upgrade_fixture:
        # An explicitly declared old-generation fixture, never an installation
        # acceptance result. The actual ECK and Stack changes below enter the
        # documented kubecli workflow with this source identity already live.
        es.validate(c);es.preflight(c,k);es.sysctl(c,k)
        es.API(str(lab.DIRECTORY/'kubectl.kubeconfig')).apply(es.resources(c,hashlib.sha256(ca.encode()).hexdigest()))
        lab.product_health(c)
        print('ELASTICSEARCH_UPGRADE_SOURCE_FIXTURE eck=3.4.1 stack=9.5.5 installation_acceptance=false',flush=True)
    else:
        lab.product('setup', lab.NAME, '07', '--tags', 'elasticsearch', timeout=5400)
    lab.bind_namespace('elastic-system')
    lab.atomic(lab.STATE/'config-compact.json', {key:value for key,value in c.items() if key.startswith('elasticsearch_')})
    lab.atomic(lab.STATE/('config-target-'+topology+'.json'), {key:value for key,value in c.items() if key.startswith('elasticsearch_')})
    if topology == 'standard':
        lab.atomic(lab.STATE/'config-standard.json', {key:value for key,value in c.items() if key.startswith('elasticsearch_')})
    lab.verify_topology('compact')


def retire_business(lab, topology):
    k = lab.api(); ns = 'es-'+topology
    lab.cleanup_namespaces()
    identities = json.loads((lab.STATE/'namespaces.json').read_text())
    namespace = k.get('namespace', ns)
    if not namespace or namespace['metadata']['uid'] != identities.get(ns):
        raise ValueError('DR business retirement namespace identity changed')
    config = json.loads((lab.STATE/('config-target-'+topology+'.json')).read_text())
    lab.configuration(**dict(config, elasticsearch_state='absent', elasticsearch_requests=[]))
    lab.product('setup', lab.NAME, '07', '--tags', 'elasticsearch', timeout=5400)
    k.run('delete', 'namespace', ns, '--wait=true', '--timeout=10m', timeout=660)
    deadline = time.monotonic()+600
    while k.get('pv')['items'] and time.monotonic()<deadline:
        print('ELASTICSEARCH_WAIT_HEARTBEAT cleanup=DR-business-PVC',flush=True);time.sleep(5)
    if k.get('pv')['items']:raise ValueError('DR target business PVs were not reclaimed')
    if k.get('namespace',ns):raise ValueError('DR target namespace retirement incomplete')
    identities.pop(ns);lab.atomic(lab.STATE/'namespaces.json',identities)
    print('ELASTICSEARCH_DR_BUSINESS_SCOPED_CLEAN_PASS topology='+topology+' platform=preserved',flush=True)


def file_realm(lab, c):
    import secrets
    k, ns = lab.api(), c['elasticsearch_namespace']
    for name in ('es-dr-file-realm','es-dr-admin'):
        if k.get('secret',name,ns): raise ValueError('DR file-realm fixture already exists; inspect its original identity')
    username, password = 'es_dr_recovery', secrets.token_urlsafe(32)
    # Match the pinned Hasher.getPbkdf2Hash format and its 256-bit output.
    # Elasticsearch does not accept Unix SHA-512 crypt ($6$) file-realm hashes.
    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac('sha512', password.encode(), salt, 100000, dklen=32)
    encoded = '{PBKDF2}100000$'+base64.b64encode(salt).decode()+'$'+base64.b64encode(derived).decode()
    k.apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':'es-dr-file-realm','namespace':ns,'labels':lab.LABELS},'type':'Opaque',
        'stringData':{'users':username+':'+encoded+'\n','users_roles':'superuser:'+username+'\n'}},
        {'apiVersion':'v1','kind':'Secret','metadata':{'name':'es-dr-admin','namespace':ns,'labels':lab.LABELS},'type':'Opaque',
        'stringData':{'username':username,'password':password}}])


def verify_documents(lab, record, *, additional=None):
    c, es, k = lab.selected_config('compact'), lab.load_es(), lab.api()
    expected = dict(record['documents'], **(additional or {}))
    with es.connection(c,k) as request:
        root = request('GET','/')
        if root['cluster_uuid'] == record['source_cluster_uuid']: raise ValueError('restore reused source Elasticsearch identity')
        request('POST','/'+record['index']+'/_refresh')
        rows = request('POST','/'+record['index']+'/_mget',{'ids':sorted(expected)})['docs']
        actual = {r['_id']:r['_source'] for r in rows if r.get('found')}
        if actual != expected or request('GET','/'+record['index']+'/_count')['count'] != len(expected):
            raise ValueError('independent target restored document count/content differs')
        query = request('POST','/'+record['index']+'/_search?allow_partial_search_results=false',
                        {'query':{'term':{'business_id':'7'}},'size':1})
        if query.get('_shards',{}).get('failed') or [hit['_source'] for hit in query['hits']['hits']] != [expected['7']]:
            raise ValueError('restored mapping/query result differs')
    digest = hashlib.sha256(json.dumps(actual,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if not additional and digest != record['sha256']: raise ValueError('independent target canonical content hash differs')
    print('ELASTICSEARCH_RESTORE_MEASUREMENT '+json.dumps({'documents':len(actual),'sha256':digest,'target_cluster_uuid':root['cluster_uuid'],
          'source_cluster_uuid':record['source_cluster_uuid']},sort_keys=True),flush=True)
    return root['cluster_uuid']


def restore(lab, topology):
    baseline(lab)
    config = json.loads((lab.STATE/('config-target-'+topology+'.json')).read_text())
    lab.atomic(lab.STATE/'config-compact.json', config)
    record = json.loads((SOURCE/('backup-'+topology+'.json')).read_text())
    store = json.loads((SOURCE/'s3.json').read_text())
    repository = lab.repository_request(topology,store)
    repository['id'] = 'dr-repository-'+topology
    c = lab.selected_config('compact',elasticsearch_requests=[repository])
    lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
    with lab.load_es().connection(c,lab.api()) as request:
        repo = request('GET','/_snapshot/gate-archive')['gate-archive']
        if str(repo['settings'].get('readonly')).lower() != 'true' or repo['settings']['base_path'] != store['owner']+'/'+topology:
            raise ValueError('DR target repository is not the exact readonly source base_path')
        snapshot = request('GET','/_snapshot/gate-archive/'+record['snapshot'])['snapshots'][0]
        if snapshot['uuid'] != record['snapshot_uuid']: raise ValueError('DR source snapshot identity changed')
    c = lab.selected_config('compact',elasticsearch_requests=[{'id':'dr-restore-'+topology,'type':'restore','repository':'gate-archive',
                            'snapshot':record['snapshot'],'indices':[record['index']]}])
    lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
    target_uuid = verify_documents(lab,record)
    lab.selected_config('compact',elasticsearch_requests=[])
    lab.atomic(lab.STATE/('restore-'+topology+'.json'),{'source_snapshot_uuid':record['snapshot_uuid'],'target_cluster_uuid':target_uuid,'phase':'complete'})
    print('ELASTICSEARCH_DR_RESTORE_PASS topology='+topology+' repository=readonly',flush=True)


def kibana_request(lab, c, method, path, body=None, *, allow_missing=False):
    """Call the actual Kibana HTTPS API with its ECK CA and explicit identity."""
    k, es = lab.api(), lab.load_es()
    ns, name = c['elasticsearch_namespace'], c['elasticsearch_name']
    cert=k.get('secret',name+'-kb-http-certs-public',ns)
    admin=k.get('secret',c['elasticsearch_admin_secret'] or name+'-es-elastic-user',ns)
    context=ssl.create_default_context(cadata=base64.b64decode(cert['data'].get('ca.crt') or cert['data']['tls.crt']).decode())
    identity=(base64.b64decode(admin['data']['username'])+b':'+base64.b64decode(admin['data']['password']) if c['elasticsearch_admin_secret']
              else b'elastic:'+base64.b64decode(admin['data']['elastic']))
    with tempfile.TemporaryFile(mode='w+') as log:
        process=subprocess.Popen(k.command+['-n',ns,'port-forward','service/'+name+'-kb-http',':5601','--address=127.0.0.1'],stdout=log,stderr=log,text=True)
        try:
            deadline=time.monotonic()+30;port=None
            while time.monotonic()<deadline and process.poll() is None:
                log.seek(0);match=re.search(r'Forwarding from 127\.0\.0\.1:([0-9]+)',log.read())
                if match:port=int(match[1]);break
                time.sleep(.2)
            if port is None:raise ValueError('Kibana HTTPS fixture tunnel did not become ready')
            conn=es.TunnelHTTPS(name+'-kb-http.'+ns+'.svc',port,timeout=60,context=context)
            try:
                conn.request(method,path,body=json.dumps(body) if body is not None else None,
                    headers={'Authorization':'Basic '+base64.b64encode(identity).decode(),'Content-Type':'application/json','kbn-xsrf':'elasticsearch-gate'})
                response=conn.getresponse();raw=response.read()
                if allow_missing and method=='GET' and response.status==404:return None
                if response.status>=300:raise ValueError('Kibana fixture API refused status='+str(response.status))
                return json.loads(raw) if raw else {}
            finally:conn.close()
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)


def restore_features(lab, record):
    """Restore system states with Kibana stopped and an independent file realm."""
    c=json.loads((lab.STATE/'config-target-standard.json').read_text())
    lab.atomic(lab.STATE/'config-compact.json',c)
    if c['elasticsearch_kibana_enabled'] or not c['elasticsearch_file_realm_secret'] or not c['elasticsearch_admin_secret']:
        raise ValueError('feature restore requires stopped Kibana and independent file-realm credentials')
    store=json.loads((SOURCE/'s3.json').read_text())
    repository=lab.repository_request('standard',store);repository['id']='feature-reader-repository'
    lab.selected_config('compact',elasticsearch_requests=[repository])
    lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
    with lab.load_es().connection(c,lab.api()) as request:
        identity=request('GET','/_security/_authenticate')
        if identity.get('authentication_realm',{}).get('type')!='file' or 'superuser' not in identity.get('roles',[]):
            raise ValueError('feature restore lacks an authenticated file-realm superuser')
        snapshot=request('GET','/_snapshot/gate-archive/'+record['snapshot'])['snapshots'][0]
        if snapshot['uuid']!=record['snapshot_uuid']:raise ValueError('feature snapshot identity changed')
    lab.selected_config('compact',elasticsearch_requests=[{'id':'feature-state-restore','type':'restore','repository':'gate-archive',
        'snapshot':record['snapshot'],'indices':[record['index']],'feature_states':['security','kibana']}])
    lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
    uuid=verify_documents(lab,record)
    c=lab.selected_config('compact',elasticsearch_requests=[],elasticsearch_kibana_enabled=True)
    lab.product('setup',lab.NAME,'07','--tags','elasticsearch',timeout=5400)
    lab.verify_topology('compact')
    return c,uuid


def cleanup_feature_source(source, c, secret, attributes):
    """Reclaim only this source owner's feature fixture, including partial setup."""
    k=source.api();name=secret['metadata']['name'];ns=c['elasticsearch_namespace']
    current=k.get('secret',name,ns)
    if (not current or current['metadata']['uid']!=secret['metadata']['uid'] or
            any(current['metadata'].get('labels',{}).get(key)!=value for key,value in source.LABELS.items())):
        raise ValueError('feature credential fixture identity changed')
    dashboard='/api/saved_objects/dashboard/'+name
    observed=kibana_request(source,c,'GET',dashboard,allow_missing=True)
    if observed and any(observed.get('attributes',{}).get(key)!=value for key,value in attributes.items()):
        raise ValueError('source dashboard identity changed before cleanup')
    with source.load_es().connection(c,k) as request:
        users=request('GET','/_security/user');roles=request('GET','/_security/role')
        if name in users and users[name].get('roles')!=[name]:raise ValueError('feature user ownership changed')
        if name in roles:
            role=roles[name];grants=role.get('indices',[])
            if (role.get('cluster') or role.get('applications') or role.get('run_as') or len(grants)!=1 or
                    grants[0].get('names')!=[source.data_ledger('standard')['index']] or grants[0].get('privileges')!=['read']):
                raise ValueError('feature role ownership changed')
        if observed:kibana_request(source,c,'DELETE',dashboard)
        # Missing native objects are normal when product preflight failed.
        if name in users:request('DELETE','/_security/user/'+name)
        if name in roles:request('DELETE','/_security/role/'+name)
    current=k.get('secret',name,ns)
    if not current or current['metadata']['uid']!=secret['metadata']['uid']:raise ValueError('feature credential fixture identity changed')
    k.run('-n',ns,'delete','secret',name,'--wait=true')
    source.data_ledger('standard')


def features(source):
    """Prove native user, saved object and protected configuration recovery."""
    import secrets
    target=target_lab();baseline(target)
    c,k,es=source.selected_config('standard'),source.api(),source.load_es()
    if not c['elasticsearch_kibana_enabled']:raise ValueError('feature backup requires the source Kibana instance')
    token=json.loads((SOURCE/'ownership.json').read_text())['id'].replace('-','')
    # A retry creates fresh native credentials. Its immutable snapshot and
    # request journals must have the same fresh attempt identity, too.
    name='es-feature-'+token[:6]+'-'+secrets.token_hex(6);ns=c['elasticsearch_namespace']
    dashboard='/api/saved_objects/dashboard/'+name
    attributes={'title':'Kubeauto recovery '+token,'description':'Owned feature-state restore fixture','panelsJSON':'[]','optionsJSON':'{}'}
    # Recover a prior partial source fixture before creating a fresh attempt.
    # Snapshot objects and product journals remain immutable evidence.
    pattern=r'es-feature-(?:'+re.escape(token[:6])+'|'+re.escape(token[:12])+r')-[a-f0-9]{12}'
    for previous in k.get('secrets',namespace=ns)['items']:
        if re.fullmatch(pattern,previous['metadata']['name']):cleanup_feature_source(source,c,previous,attributes)
    if k.get('secret',name,ns):raise ValueError('feature backup fixture already exists')
    # Record protected configuration independently: ES snapshots exclude these
    # Kubernetes objects and node configuration. Never print their contents.
    secret_names={c[key] for key in c if key.endswith('_secret') and c[key]}
    secret_names.update((c['elasticsearch_name']+'-es-elastic-user',c['elasticsearch_name']+'-es-http-certs-public'))
    protected={'config':c,'secrets':[k.get('secret',name,ns) for name in sorted(secret_names)],
               'configmaps':[k.get('configmap',c['elasticsearch_s3_ca_configmap'],ns)]}
    if any(obj is None for key in ('secrets','configmaps') for obj in protected[key]):
        raise ValueError('feature protection inventory has missing configuration references')
    source.atomic(SOURCE/'feature-protected-config.json',protected)
    password=secrets.token_urlsafe(32)
    k.apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':ns,'labels':source.LABELS},'type':'Opaque',
              'stringData':{'username':name,'password':password}}])
    secret=k.get('secret',name,ns)
    try:
        requests=[{'id':name+'-role','type':'role','name':name,'definition':{'cluster':[],
                    'indices':[{'names':[source.data_ledger('standard')['index']],'privileges':['read']}]}},
                  {'id':name+'-user','type':'user','name':name,'password_secret':name,'roles':[name]}]
        source.selected_config('standard',elasticsearch_requests=requests)
        source.product('setup',source.NAME,'07','--tags','elasticsearch',timeout=5400)
        result=kibana_request(source,c,'POST',dashboard,{'attributes':attributes})
        if result.get('id')!=name or any(result.get('attributes',{}).get(key)!=value for key,value in attributes.items()):
            raise ValueError('source saved object creation differs')
        attributes=result['attributes']
        ledger=source.data_ledger('standard')
        source.selected_config('standard',elasticsearch_requests=[{'id':name+'-snapshot','type':'snapshot','repository':'gate-archive',
            'snapshot':name,'indices':[ledger['index']],'feature_states':['security','kibana']}])
        source.product('setup',source.NAME,'07','--tags','elasticsearch',timeout=5400)
        with es.connection(c,k) as request:
            snapshot=request('GET','/_snapshot/gate-archive/'+name)['snapshots'][0]
            states={state['feature_name']:state.get('indices',[]) for state in snapshot.get('feature_states',[])}
            if snapshot['state']!='SUCCESS' or any(not states.get(f) for f in ('security','kibana')):
                raise ValueError('source snapshot lacks successful nonempty security/Kibana feature states')
        original=json.loads((SOURCE/'backup-standard.json').read_text())
        record=dict(original,snapshot=name,snapshot_uuid=snapshot['uuid'])
        source.atomic(SOURCE/'feature-backup.json',record)
        for topology in ('standard','compact'):
            if target.api().get('namespace','es-'+topology):retire_business(target,topology)
        install(target,'standard',features=True)
        tc,target_uuid=restore_features(target,record)
        target.api().apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':ns,'labels':target.LABELS},'type':'Opaque','data':secret['data']}])
        with es.connection(dict(tc,elasticsearch_admin_secret=name),target.api()) as request:
            identity=request('GET','/_security/_authenticate')
            if identity.get('username')!=name or identity.get('authentication_realm',{}).get('type')!='native' or identity.get('roles')!=[name]:
                raise ValueError('restored native user/role cannot authenticate')
            if request('GET','/'+ledger['index']+'/_count')['count']!=len(ledger['documents']):
                raise ValueError('restored native reader cannot access its exact business ledger')
        observed=kibana_request(target,tc,'GET',dashboard)
        if observed.get('id')!=name or observed.get('attributes')!=attributes:raise ValueError('restored Kibana saved object differs')
        saved=json.loads((SOURCE/'feature-protected-config.json').read_text())
        if saved!=protected:raise ValueError('independent configuration/Secret/CA archive changed')
        for kind,key in (('secret','secrets'),('configmap','configmaps')):
            for original in saved[key]:
                current=k.get(kind,original['metadata']['name'],ns)
                if not current or current['metadata']['uid']!=original['metadata']['uid'] or current.get('data')!=original.get('data'):
                    raise ValueError('feature restore altered protected source configuration or credentials')
        source.atomic(SOURCE/'feature-restore.json',{'snapshot_uuid':snapshot['uuid'],'target_cluster_uuid':target_uuid,
            'native_identity_verified':True,'saved_object_verified':True,'protected_archive_sha256':hashlib.sha256((SOURCE/'feature-protected-config.json').read_bytes()).hexdigest()})
    finally:
        primary=sys.exc_info()[1]
        try:
            source.selected_config('standard',elasticsearch_requests=[])
            cleanup_feature_source(source,c,secret,attributes)
        except Exception as error:
            if primary is None:raise
            print('ELASTICSEARCH_FEATURE_SOURCE_CLEANUP_FAIL reason='+type(error).__name__,file=sys.stderr)
    print('ELASTICSEARCH_CASE_PASS id=ES-42',flush=True)
    print('ELASTICSEARCH_FEATURE_RESTORE_SCOPED_CLEAN_PASS target=isolated native_user=verified saved_object=verified protected_config=verified',flush=True)


def previous_eck(lab):
    """An empty, independent target may host the declared old source fixture."""
    spec=importlib.util.spec_from_file_location('es_previous_eck',BASE/'roles/cluster-addon/files/eck.py')
    eck=importlib.util.module_from_spec(spec);spec.loader.exec_module(eck)
    api=eck.API(str(lab.DIRECTORY/'kubectl.kubeconfig'))
    if eck.consumers(api):raise ValueError('previous-generation ECK fixture requires zero Elastic consumers')
    pins={'crds':'488f2eda4b0d21637d0a2764f0fced6eafbb7279d1a1fb9c35a9ebaac28e393a',
          'operator':'acb8a565a3ad932e10091da32f9d5652de390ade105e8b6028314d3140576b8a'}
    for kind,checksum in pins.items():
        if hashlib.sha256((BASE/'tests/fixtures/elasticsearch'/('eck-3.4.1-'+kind+'.yaml')).read_bytes()).hexdigest()!=checksum:
            raise ValueError('previous ECK official fixture checksum differs')
    crds=[crd for crd in api.get('crd')['items'] if crd['spec']['group'].endswith('.k8s.elastic.co')]
    if any(not any(m.get('manager')=='kubeauto-eck-crd' for m in crd['metadata'].get('managedFields',[])) for crd in crds):
        raise ValueError('previous ECK fixture refuses a foreign CRD')
    controller=api.get('statefulset','elastic-operator','elastic-system')
    if controller:
        manager=next((c for c in controller['spec']['template']['spec']['containers'] if c.get('name')=='manager'),{})
        previous_image='registry.talkschool.cn:5000/brinnatt/eck-operator:3.4.1'
        if manager.get('image')==previous_image:
            eck.VERSION='3.4.1';eck.ROOT=BASE/'tests/fixtures/elasticsearch';eck.PINS=pins
            eck.ensure(api,'es-compact',previous_image)
            lab.bind_namespace('elastic-system')
            print('ELASTICSEARCH_UPGRADE_SOURCE_OPERATOR_REUSED version=3.4.1 consumers=0',flush=True)
            return
        eck.preflight(api,'es-compact','registry.talkschool.cn:5000/brinnatt/eck-operator:3.5.0')
        api.run('delete','namespace','elastic-system','--wait=true','--timeout=10m',timeout=660)
        identities=json.loads((lab.STATE/'namespaces.json').read_text());identities.pop('elastic-system',None)
        lab.atomic(lab.STATE/'namespaces.json',identities)
    # Do not downgrade installed CRDs. The previous fixture starts with none.
    for crd in crds:
        api.run('delete','crd',crd['metadata']['name'],'--wait=true','--timeout=120s')
    eck.VERSION='3.4.1';eck.ROOT=BASE/'tests/fixtures/elasticsearch'
    eck.PINS=pins
    lab.product('download','-E','elasticsearch-qualification',timeout=5400)
    eck.ensure(api,'es-compact','registry.talkschool.cn:5000/brinnatt/eck-operator:3.4.1')
    lab.bind_namespace('elastic-system')


def retire_upgrade_fixture(target, topology):
    """A retired upgrade generation no longer owns a live confirmed index."""
    path=target.STATE/('config-target-'+topology+'.json')
    config=json.loads(path.read_text())
    active=json.loads((target.STATE/('config-'+topology+'.json')).read_text())
    version=active['elasticsearch_ver']
    if (version not in ('9.5.5','9.5.6') or any(active[key]!=config[key] for key in
            ('elasticsearch_name','elasticsearch_namespace'))):
        raise ValueError('upgrade retirement configuration identity changed')
    current=target.api().get('elasticsearch',config['elasticsearch_name'],config['elasticsearch_namespace'])
    if current and current['spec']['version']!=version:
        raise ValueError('upgrade retirement version differs from the active fixture')
    # Installation saved the old generation. Native uninstall still validates
    # version ceilings, so hand it the verified current version after upgrade.
    target.atomic(path,dict(config,elasticsearch_ver=version))
    retire_business(target,topology)
    (target.STATE/('ledger-'+topology+'.json')).unlink(missing_ok=True)


def upgrade_snapshot(request, index):
    """SnapshotInfo exposes IndexVersion, whose release label may be a range."""
    snapshot=request('GET','/_snapshot/gate-archive/pre-upgrade')['snapshots'][0]
    nodes=request('GET','/_nodes?filter_path=nodes.*.version,nodes.*.index_version')['nodes']
    shards=snapshot.get('shards',{})
    if (len(nodes)!=3 or any(node.get('version')!='9.5.5' or
            not isinstance(node.get('index_version'),int) or node['index_version']<=0 or
            node['index_version']!=snapshot.get('version_id') for node in nodes.values()) or
            snapshot.get('state')!='SUCCESS' or not snapshot.get('uuid') or snapshot.get('indices')!=[index] or
            shards.get('failed')!=0 or shards.get('total',0)<=0 or shards.get('successful')!=shards.get('total')):
        raise ValueError('rollback source is not a successful pre-upgrade snapshot')
    return snapshot


def upgrade_logging_connection(lab):
    spec = importlib.util.spec_from_file_location('es_upgrade_logging_transport',BASE/'tests/helpers/elasticsearch-coexistence.py')
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.logging_connection(lab)


def resize_upgrade_kibana(lab, desired):
    """Change only our retained lab request; preserve ECK's atomic container list."""
    k=lab.api();obj=k.get('kibana','logging','logging')
    labels={'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'logging-efk'}
    if (not obj or obj['spec'].get('version')!='9.5.1' or obj['spec'].get('count')!=2 or
            any(obj['metadata'].get('labels',{}).get(key)!=value for key,value in labels.items())):
        raise ValueError('upgrade fixture resize refuses a foreign Kibana consumer')
    containers=obj['spec']['podTemplate']['spec']['containers']
    position=next((i for i,item in enumerate(containers) if item.get('name')=='kibana'),None)
    if position is None:raise ValueError('upgrade fixture lacks its Kibana container')
    old=containers[position].get('resources',{})
    if any(old.get(key,{}).get('memory') not in (None,'2Gi',desired[key]['memory']) for key in ('requests','limits')):
        raise ValueError('upgrade fixture resize refuses unrelated memory changes')
    replacement={key:dict(value) for key,value in old.items()}
    for key,value in desired.items():replacement.setdefault(key,{}).update(value)
    if replacement==old:return
    patch=[{'op':'test','path':'/metadata/uid','value':obj['metadata']['uid']},
           {'op':'test','path':'/metadata/resourceVersion','value':obj['metadata']['resourceVersion']},
           {'op':'test','path':'/spec/podTemplate/spec/containers','value':containers},
           {'op':'add','path':'/spec/podTemplate/spec/containers/'+str(position)+'/resources','value':replacement}]
    k.run('-n','logging','patch','kibana','logging','--type=json',
          '--field-manager=kubeauto-logging-efk','-p',json.dumps(patch))


def upgrade_logging_fixture(lab, *, retained=False):
    """An old-generation EFK consumer, not an EFK installation acceptance."""
    from jinja2 import Environment, StrictUndefined
    import yaml
    k, es = lab.api(), lab.load_es()
    namespace = 'logging'
    existing=k.get('namespace',namespace)
    labels = dict(lab.LABELS,**{'kubeauto.io/component':'logging'})
    if existing:
        owners=json.loads((lab.STATE/'namespaces.json').read_text())
        if (not retained or owners.get(namespace)!=existing['metadata']['uid'] or
                any(existing['metadata'].get('labels',{}).get(key)!=value for key,value in labels.items())):
            raise ValueError('platform upgrade refuses an unbound retained EFK fixture')
    c = lab.configuration()
    store = json.loads((SOURCE/'s3.json').read_text())
    values = dict(c,logging_namespace=namespace,logging_elasticsearch_ver='9.5.1',logging_kibana_ver='9.5.1',
        logging_efk_es_nodes=list(NODES),logging_efk_storage_class='es-test',logging_efk_es_pvc_size='4Gi',
        logging_efk_snapshot_secret='logging-upgrade-s3',logging_efk_snapshot_endpoint=store['endpoint'])
    environment = Environment(undefined=StrictUndefined)
    environment.filters.update(quote=json.dumps,bool=bool,to_json=json.dumps)
    objects = [obj for obj in yaml.safe_load_all(environment.from_string(
        (BASE/'roles/cluster-addon/templates/logging/efk.yaml.j2').read_text()).render(**values))
        if obj and obj['kind'] in ('Elasticsearch','Kibana')]
    if [obj['kind'] for obj in objects] != ['Elasticsearch','Kibana']:
        raise ValueError('platform upgrade EFK fixture differs from the delivered template')
    # Only the lab consumer declares a smaller request. Keep the delivered EFK
    # template/defaults and its three 8Gi data members unchanged. ECK otherwise
    # defaults Kibana's request to its 2Gi limit, invalidating a 1Gi budget.
    kibana=objects[1]['spec']['podTemplate']['spec']['containers'][0]
    kibana['resources']={'requests':{'memory':'1Gi'},'limits':{'memory':'2Gi'}}
    member_memory=es.quantity(objects[0]['spec']['nodeSets'][0]['podTemplate']['spec']['containers'][0]['resources']['requests']['memory'])
    kibana_memory=es.quantity(kibana['resources']['requests']['memory'])
    free = {node['metadata']['name']:es.quantity(node['status']['allocatable']['memory'])
            for node in k.get('nodes')['items'] if node['metadata']['name'] in NODES}
    for pod in k.get('pods')['items']:
        if existing and pod.get('metadata',{}).get('namespace')==namespace:continue
        node = pod['spec'].get('nodeName')
        if node not in free or pod.get('status',{}).get('phase') in ('Succeeded','Failed'):continue
        def memory(container):return es.quantity(container.get('resources',{}).get('requests',{}).get('memory','0'))
        free[node] -= max(sum(map(memory,pod['spec'].get('containers',[]))),
                          max([memory(c) for c in pod['spec'].get('initContainers',[])]+[0]))
        free[node] -= es.quantity(pod['spec'].get('overhead',{}).get('memory','0'))
    if len(free)!=3 or min(free.values())<member_memory or sum(
            value>=member_memory+kibana_memory for value in free.values())<objects[1]['spec']['count']:
        raise ValueError('platform upgrade lacks capacity for three EFK members and two Kibana replicas')
    if not existing:k.apply([{'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace,'labels':labels}}])
    lab.bind_namespace(namespace)
    credential=k.get('secret','logging-upgrade-s3',namespace)
    if credential:
        if not existing or any(credential['metadata'].get('labels',{}).get(key)!=value for key,value in labels.items()):
            raise ValueError('platform upgrade refuses foreign fixture credentials')
    else:
        if existing:raise ValueError('platform upgrade retained fixture lost its credentials')
        k.apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':'logging-upgrade-s3','namespace':namespace,'labels':labels},
            'type':'Opaque','stringData':{'s3.client.default.access_key':'logging'+secrets.token_hex(12),
                                       's3.client.default.secret_key':secrets.token_urlsafe(32)}}])
    if existing:
        resize_upgrade_kibana(lab,kibana['resources'])
    else:
        es.API(str(lab.DIRECTORY/'kubectl.kubeconfig'),field_manager='kubeauto-logging-efk').apply(objects)
    deadline = time.monotonic()+1800
    while time.monotonic()<deadline:
        ready = []
        for kind in ('elasticsearch','kibana'):
            obj = k.get(kind,'logging',namespace)
            status = (obj or {}).get('status',{})
            ready.append(bool(obj and status.get('health')=='green' and
                status.get('observedGeneration')==obj['metadata']['generation'] and
                (kind!='elasticsearch' or status.get('phase')=='Ready' and status.get('availableNodes')==3)))
        if all(ready):break
        print('ELASTICSEARCH_WAIT_HEARTBEAT upgrade=efk-source-consumer ready='+str(sum(ready))+'/2',flush=True)
        time.sleep(10)
    else:raise ValueError('platform upgrade EFK fixture did not become healthy')
    if (lab.STATE/'upgrade-logging.json').exists():
        record=json.loads((lab.STATE/'upgrade-logging.json').read_text())
        verify_upgrade_logging(lab,record)
        return record
    token = secrets.token_hex(8);index = 'es-platform-'+token;document = {'token':token}
    with upgrade_logging_connection(lab) as request:
        request('PUT','/'+index,{'settings':{'number_of_shards':1,'number_of_replicas':1}})
        result = request('PUT','/'+index+'/_create/sentinel?wait_for_active_shards=all',document)
        if result.get('_shards',{}).get('successful',0)<2 or result['_shards']['failed']:
            raise ValueError('platform upgrade EFK sentinel lacks replicated acknowledgement')
        cluster_uuid = request('GET','/')['cluster_uuid']
    credential = k.get('secret','logging-upgrade-s3',namespace)
    record = {'namespace':namespace,'namespace_uid':k.get('namespace',namespace)['metadata']['uid'],
        'cr_uids':{kind:k.get(kind,'logging',namespace)['metadata']['uid'] for kind in ('elasticsearch','kibana')},
        'pvc_uids':{p['metadata']['name']:p['metadata']['uid'] for p in k.get('pvc',namespace=namespace)['items']},
        'secret_uid':credential['metadata']['uid'],
        'secret_sha256':hashlib.sha256(json.dumps(credential['data'],sort_keys=True).encode()).hexdigest(),
        'cluster_uuid':cluster_uuid,'index':index,'document':document}
    lab.atomic(lab.STATE/'upgrade-logging.json',record)
    print('ELASTICSEARCH_UPGRADE_EFK_SOURCE_FIXTURE stack=9.5.1 members=3 kibana=2 installation_acceptance=false',flush=True)
    return record


def verify_upgrade_logging(lab, record):
    k, es = lab.api(), lab.load_es();namespace = record['namespace']
    if (k.get('namespace',namespace)['metadata']['uid']!=record['namespace_uid'] or
            {kind:k.get(kind,'logging',namespace)['metadata']['uid'] for kind in ('elasticsearch','kibana')}!=record['cr_uids'] or
            {p['metadata']['name']:p['metadata']['uid'] for p in k.get('pvc',namespace=namespace)['items']}!=record['pvc_uids'] or
            k.get('secret','logging-upgrade-s3',namespace)['metadata']['uid']!=record['secret_uid'] or
            hashlib.sha256(json.dumps(k.get('secret','logging-upgrade-s3',namespace)['data'],sort_keys=True).encode()).hexdigest()!=record['secret_sha256']):
        raise ValueError('platform upgrade changed EFK persistent identities')
    with upgrade_logging_connection(lab) as request:
        info = request('GET','/')
        request('POST','/'+record['index']+'/_refresh')
        if (info['cluster_uuid']!=record['cluster_uuid'] or info['version']['number']!='9.5.1' or
                request('GET','/'+record['index']+'/_doc/sentinel')['_source']!=record['document'] or
                request('GET','/'+record['index']+'/_count')['count']!=1):
            raise ValueError('platform upgrade changed EFK business data or version')
    print('ELASTICSEARCH_UPGRADE_EFK_VERIFIED version=9.5.1 confirmed_documents=1',flush=True)


def retire_upgrade_logging(lab, record):
    verify_upgrade_logging(lab,record)
    k = lab.api();namespace = record['namespace'];obj = k.get('namespace',namespace)
    if any(obj['metadata'].get('labels',{}).get(key)!=value for key,value in lab.LABELS.items()):
        raise ValueError('platform upgrade EFK cleanup ownership changed')
    options = {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{
        'uid':record['namespace_uid'],'resourceVersion':obj['metadata']['resourceVersion']}}
    k.run('delete','--raw','/api/v1/namespaces/'+namespace,'-f','-',body=json.dumps(options))
    k.run('wait','--for=delete','namespace/'+namespace,'--timeout=10m',timeout=660)
    deadline = time.monotonic()+600
    while time.monotonic()<deadline:
        if not any(p.get('spec',{}).get('claimRef',{}).get('namespace')==namespace for p in k.get('pv')['items']):break
        print('ELASTICSEARCH_WAIT_HEARTBEAT cleanup=upgrade-efk-PVC',flush=True);time.sleep(5)
    else:raise ValueError('platform upgrade EFK fixture retains bound volumes')
    forget_retired_upgrade_logging(lab,record)
    print('ELASTICSEARCH_UPGRADE_EFK_SCOPED_CLEAN_PASS business=preserved',flush=True)


def forget_retired_upgrade_logging(lab, record):
    """Archive the retired generation before releasing its active identities."""
    namespace=record['namespace'];k=lab.api()
    if k.get('namespace',namespace) or any(
            obj.get('spec',{}).get('claimRef',{}).get('namespace')==namespace for obj in k.get('pv')['items']):
        raise ValueError('retired upgrade fixture still has live namespace or storage')
    path=lab.STATE/'upgrade-logging.json';owners_path=lab.STATE/'namespaces.json'
    owners=json.loads(owners_path.read_text())
    archive=lab.STATE/('retired-upgrade-logging-'+hashlib.sha256(record['namespace_uid'].encode()).hexdigest()+'.json')
    if (json.loads(path.read_text())!=record or owners.get(namespace) not in (None,record['namespace_uid']) or
            (owners.get(namespace) is None and not archive.exists()) or
            (archive.exists() and json.loads(archive.read_text())!=record)):
        raise ValueError('retired upgrade fixture identity records changed')
    lab.atomic(archive,record)
    owners.pop(namespace,None);lab.atomic(owners_path,owners)
    path.unlink()


def recover_retired_upgrade_logging(lab):
    """Recover the old gate's stale record, including its empty failed retry.

    Never adopt a replacement. Only an empty namespace created by this fixed
    fixture manager can be removed; discover all listable namespaced kinds so
    an unknown resource also blocks cleanup. Healthy old business is retained.
    """
    path=lab.STATE/'upgrade-logging.json'
    if not path.exists():return
    record=json.loads(path.read_text());namespace=record['namespace'];k=lab.api()
    current=k.get('namespace',namespace)
    if current and current['metadata']['uid']==record['namespace_uid']:return
    owners=json.loads((lab.STATE/'namespaces.json').read_text())
    if namespace!='logging' or owners.get(namespace)!=record['namespace_uid']:
        raise ValueError('retired upgrade recovery ownership changed')
    if any(obj.get('spec',{}).get('claimRef',{}).get('namespace')==namespace for obj in k.get('pv')['items']):
        raise ValueError('retired upgrade recovery retains storage')
    if current:
        observed=current['metadata']['uid']
        current=json.loads(k.run('get','namespace',namespace,'-o','json','--show-managed-fields=true'))
        if current['metadata']['uid']!=observed:raise ValueError('retired upgrade recovery namespace changed during discovery')
        meta=current['metadata'];labels=dict(lab.LABELS,**{'kubeauto.io/component':'logging'})
        if (any(meta.get('labels',{}).get(key)!=value for key,value in labels.items()) or
                not any(item.get('manager')=='kubeauto-elasticsearch-gate' and item.get('operation')=='Apply'
                        for item in meta.get('managedFields',[]))):
            raise ValueError('retired upgrade recovery refuses foreign namespace')
        kinds=k.run('api-resources','--namespaced=true','--verbs=list','-o','name').splitlines()
        if not {'secrets','pods','persistentvolumeclaims','configmaps','serviceaccounts'}<=set(kinds):
            raise ValueError('retired upgrade recovery resource discovery is incomplete')
        for kind in kinds:
            objects=json.loads(k.run('-n',namespace,'get',kind,'-o','json'))['items']
            allowed={'configmaps':{'kube-root-ca.crt'},'serviceaccounts':{'default'}}.get(kind,set())
            if any(obj['metadata']['name'] not in allowed for obj in objects):
                raise ValueError('retired upgrade recovery refuses nonempty namespace: '+kind)
        options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{
            'uid':meta['uid'],'resourceVersion':meta['resourceVersion']}}
        k.run('delete','--raw','/api/v1/namespaces/'+namespace,'-f','-',body=json.dumps(options))
        k.run('wait','--for=delete','namespace/'+namespace,'--timeout=120s',timeout=150)
    forget_retired_upgrade_logging(lab,record)
    print('ELASTICSEARCH_UPGRADE_RETIRED_FIXTURE_RECOVERED empty_retry=removed old_record=archived',flush=True)


def admission_patch(k, obj, changes):
    metadata=obj['metadata']
    patch=[{'op':'test','path':'/metadata/uid','value':metadata['uid']},
           {'op':'test','path':'/metadata/resourceVersion','value':metadata['resourceVersion']}]
    patch.extend({'op':'add','path':path,'value':value} for path,value in changes.items())
    k.run('-n','logging','patch','kibana','logging','--type=json','-p',json.dumps(patch))


def restore_upgrade_admission(lab):
    path=lab.STATE/'upgrade-admission.json'
    if not path.exists():return
    record=json.loads(path.read_text());k=lab.api();obj=k.get('kibana','logging','logging')
    if not obj or obj['metadata']['uid']!=record['uid']:
        raise ValueError('platform admission recovery refuses a changed consumer')
    labels=record['labels'];annotations=record['annotations'];count=record['count']
    allowed_labels=[labels,dict(labels,**{'kubeauto.io/component':'foreign-fixture'})]
    allowed_annotations=[annotations,dict(annotations,**{'eck.k8s.elastic.co/managed':'false'})]
    if (obj['metadata'].get('labels',{}) not in allowed_labels or
            obj['metadata'].get('annotations',{}) not in allowed_annotations or obj['spec']['count'] not in (count,count+1)):
        raise ValueError('platform admission recovery refuses unrelated consumer changes')
    admission_patch(k,obj,{'/metadata/labels':labels,'/metadata/annotations':annotations,'/spec/count':count})
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:
        obj=k.get('kibana','logging','logging');status=(obj or {}).get('status',{})
        if (obj and obj['metadata']['uid']==record['uid'] and status.get('health')=='green' and
                status.get('observedGeneration')==obj['metadata']['generation']):break
        print('ELASTICSEARCH_WAIT_HEARTBEAT upgrade=admission-consumer-recovery',flush=True);time.sleep(5)
    else:raise ValueError('platform admission consumer did not recover')
    if record.get('journal'):
        name,uid=record['journal']['name'],record['journal']['uid'];obj=k.get('configmap',name,'elastic-system')
        if (not obj or obj['metadata']['uid']!=uid or obj['data'].get('fingerprint')!='invalid-fixture-input' or
                obj['metadata'].get('labels',{}).get('kubeauto.io/component')!='eck'):
            raise ValueError('platform admission journal identity changed before cleanup')
        options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid,'resourceVersion':obj['metadata']['resourceVersion']}}
        k.run('delete','--raw','/api/v1/namespaces/elastic-system/configmaps/'+name,'-f','-',body=json.dumps(options))
    path.unlink()


def upgrade_admission(lab, logging):
    """Disprove unsafe platform starts through the platform's actual CLI."""
    k=lab.api();original=lab.configuration();path=lab.STATE/'upgrade-admission.json'
    if path.exists():restore_upgrade_admission(lab)
    consumer=k.get('kibana','logging','logging')
    record={'uid':consumer['metadata']['uid'],'labels':consumer['metadata'].get('labels',{}),
            'annotations':consumer['metadata'].get('annotations',{}),'count':consumer['spec']['count']}
    def platform_identity():
        controller=k.get('statefulset','elastic-operator','elastic-system')
        return {'operator':{'uid':controller['metadata']['uid'],'spec':controller['spec']},
                'crds':{obj['metadata']['name']:{'uid':obj['metadata']['uid'],'spec':obj['spec']}
                        for obj in k.get('crd')['items'] if obj['spec']['group'].endswith('.k8s.elastic.co')}}
    before=platform_identity();business=lab.topology_state('compact')
    lab.atomic(path,record)
    try:
        lab.configuration(eck_platform_upgrade_from='3.4.1',eck_platform_change_id='admission-'+secrets.token_hex(6))
        admission_patch(k,consumer,{'/metadata/labels':dict(record['labels'],**{'kubeauto.io/component':'foreign-fixture'})})
        lab.product_refusal('unqualified Elastic consumer','platform-unqualified',tags='eck-platform',marker='ECK_SHARED_REFUSED')
        restore_upgrade_admission(lab)
        lab.atomic(path,record)
        # ECK's official unmanaged annotation keeps the running fixture intact
        # while a real spec generation is pending. No fabricated status is used.
        consumer=k.get('kibana','logging','logging')
        admission_patch(k,consumer,{'/metadata/annotations':dict(record['annotations'],**{'eck.k8s.elastic.co/managed':'false'}),
                                   '/spec/count':record['count']+1})
        pending=k.get('kibana','logging','logging')
        if pending.get('status',{}).get('observedGeneration')==pending['metadata']['generation']:
            raise ValueError('platform admission fault did not create a pending generation')
        lab.product_refusal('every consumer to be healthy','platform-unhealthy',tags='eck-platform',marker='ECK_SHARED_REFUSED')
        restore_upgrade_admission(lab)
        change='admission-conflict-'+secrets.token_hex(6);name='kubeauto-eck-change-'+change
        if k.get('configmap',name,'elastic-system'):raise ValueError('platform admission change already exists')
        k.apply([{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':'elastic-system',
            'labels':{'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'eck'}},
            'data':{'phase':'running','fingerprint':'invalid-fixture-input','inputs':'{}'}}])
        lab.atomic(path,dict(record,journal={'name':name,'uid':k.get('configmap',name,'elastic-system')['metadata']['uid']}))
        lab.configuration(eck_platform_change_id=change)
        lab.product_refusal('ECK change ID is bound to different input','platform-change-conflict',tags='eck-platform',marker='ECK_SHARED_REFUSED')
        verify_upgrade_logging(lab,logging);lab.verify_topology('compact');lab.data_ledger('compact')
        if before!=platform_identity() or business!=lab.topology_state('compact'):
            raise ValueError('refused platform maintenance altered a protected identity')
    finally:
        lab.configuration(**original)
        restore_upgrade_admission(lab)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-50-platform-admission',flush=True)


def reuse_upgrade_source(target):
    """Reuse only a healthy owned old generation before platform mutation."""
    k=target.api()
    logging=k.get('namespace','logging')
    owners=json.loads((target.STATE/'namespaces.json').read_text())
    c=target.selected_config('compact')
    operator=k.get('statefulset','elastic-operator','elastic-system')
    image=next((item.get('image') for item in (operator or {}).get('spec',{}).get('template',{}).get('spec',{}).get('containers',[])
                if item.get('name')=='manager'),None)
    if not logging and image!='registry.talkschool.cn:5000/brinnatt/eck-operator:3.4.1':return False
    if (image!='registry.talkschool.cn:5000/brinnatt/eck-operator:3.4.1' or c['elasticsearch_ver']!='9.5.5' or
            c['elasticsearch_namespace']!='es-compact' or list(target.STATE.glob('fault-*.json')) or
            any(not (obj:=k.get('namespace',name)) or obj['metadata']['uid']!=owners.get(name)
                for name in (('logging','es-compact') if logging else ('es-compact',)))):
        raise ValueError('upgrade continuation requires its owned healthy pre-upgrade generation')
    target.cleanup_namespaces();target.verify_topology('compact');target.data_ledger('compact')
    print('ELASTICSEARCH_UPGRADE_SOURCE_REUSED eck=3.4.1 stack=9.5.5 confirmed_data=verified',flush=True)
    return True


def upgrade(source, *, platform_only=False):
    """Upgrade an independent old generation and restore its pre-upgrade point."""
    from concurrent.futures import ThreadPoolExecutor
    target=target_lab();baseline(target)
    restore_upgrade_admission(target)
    recover_retired_upgrade_logging(target)
    retained=reuse_upgrade_source(target)
    if not retained:
        for topology in ('standard','compact'):
            if target.api().get('namespace','es-'+topology):retire_upgrade_fixture(target,topology)
        previous_eck(target)
        install(target,'compact',version='9.5.5',upgrade_fixture=True)
    k,es=target.api(),target.load_es()
    target.data_ledger('compact',create=True)
    logging = upgrade_logging_fixture(target,retained=retained)
    upgrade_admission(target,logging)
    before=target.topology_state('compact');change='gate-upgrade-'+secrets.token_hex(6)
    operator_uid=k.get('statefulset','elastic-operator','elastic-system')['metadata']['uid']
    c=target.selected_config('compact')
    target.configuration(eck_platform_upgrade_from='3.4.1',eck_platform_change_id=change)
    def fault_during_upgrade():
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            journal=k.get('configmap','kubeauto-eck-change-'+change,'elastic-system')
            if journal and journal.get('data',{}).get('phase')=='running':
                pod=k.get('pod','elastic-operator-0','elastic-system')
                if not pod:time.sleep(.2);continue
                if not any(owner.get('uid')==operator_uid for owner in pod['metadata'].get('ownerReferences',[])):
                    raise ValueError('ECK upgrade fault refuses an unproven Operator Pod owner')
                options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':pod['metadata']['uid'],'resourceVersion':pod['metadata']['resourceVersion']}}
                k.run('delete','--raw','/api/v1/namespaces/elastic-system/pods/elastic-operator-0','-f','-',body=json.dumps(options))
                return pod['metadata']['uid']
            if journal and journal.get('data',{}).get('phase')=='complete':break
            time.sleep(.2)
        raise ValueError('ECK upgrade lacks its declared concurrent Operator fault')
    with ThreadPoolExecutor(max_workers=1) as pool:
        injected=pool.submit(fault_during_upgrade)
        target.product('setup',target.NAME,'07','--tags','eck-platform',timeout=5400)
        fault_uid=injected.result()
    target.product('setup',target.NAME,'07','--tags','eck-platform',timeout=5400)
    target.configuration(eck_platform_upgrade_from='',eck_platform_change_id='')
    target.product('setup',target.NAME,'07','--tags','elasticsearch',timeout=5400)
    target.verify_topology('compact');ledger=target.data_ledger('compact')
    verify_upgrade_logging(target,logging)
    after=target.topology_state('compact')
    if any(before[key]!=after[key] for key in ('cr_uid','pvc_uids','credentials_uid','credentials_sha256')):
        raise ValueError('ECK upgrade altered preserved consumer/data/credential identities')
    pod=k.get('pod','elastic-operator-0','elastic-system')
    if (pod['metadata']['uid']==fault_uid or
            not any(owner.get('uid')==operator_uid for owner in pod['metadata'].get('ownerReferences',[])) or
            not any(condition.get('type')=='Ready' and condition.get('status')=='True' for condition in pod.get('status',{}).get('conditions',[]))):
        raise ValueError('ECK upgrade Operator fault did not recover under its original owner')
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-50-platform-upgrade',flush=True)
    print('ELASTICSEARCH_SUBCASE_PASS id=ES-50-platform-recovery',flush=True)
    print('ELASTICSEARCH_CASE_PASS id=ES-50',flush=True)
    if platform_only:
        retire_upgrade_logging(target,logging)
        target.verify_topology('compact');target.data_ledger('compact')
        for topology in ('standard','compact'):source.verify_topology(topology);source.data_ledger(topology)
        print('ELASTICSEARCH_PLATFORM_UPGRADE_SCOPED_CLEAN_PASS fault=recovered fixture=retired source=unchanged',flush=True)
        return
    store=json.loads((SOURCE/'s3.json').read_text());repository=target.repository_request('upgrade',store);repository['id']='upgrade-source-repository'
    c=target.selected_config('compact',elasticsearch_eck_upgrade_from='',elasticsearch_eck_upgrade_change_id='',elasticsearch_repository_role='writer',
        elasticsearch_requests=[repository,{'id':'pre-upgrade-backup','type':'snapshot','repository':'gate-archive','snapshot':'pre-upgrade',
                                          'indices':[ledger['index']]}])
    target.product('setup',target.NAME,'07','--tags','elasticsearch',timeout=5400)
    with es.connection(c,k) as request:
        snapshot=upgrade_snapshot(request,ledger['index'])
        source_uuid=request('GET','/')['cluster_uuid']
    backup={'snapshot':'pre-upgrade','snapshot_uuid':snapshot['uuid'],'index':ledger['index'],'documents':ledger['documents'],
            'sha256':ledger['sha256'],'source_cluster_uuid':source_uuid}
    target.atomic(target.STATE/'upgrade-backup.json',backup)
    target.selected_config('compact',elasticsearch_ver='9.5.6',elasticsearch_requests=[])
    target.product('setup',target.NAME,'07','--tags','elasticsearch',timeout=5400)
    target.verify_topology('compact');target.data_ledger('compact')
    if target.topology_state('compact')['pvc_uids']!=before['pvc_uids']:raise ValueError('Stack rolling upgrade replaced persistent claims')
    if kibana_request(target,target.selected_config('compact'),'GET','/api/status')['version']['number']!='9.5.6':
        raise ValueError('Kibana did not converge to the upgraded version')
    verify_upgrade_logging(target,logging)
    retire_upgrade_logging(target,logging)
    print('ELASTICSEARCH_CASE_PASS id=ES-51',flush=True)
    target.selected_config('compact',elasticsearch_ver='9.5.5')
    target.product_refusal('in-place Elasticsearch downgrade is forbidden','stack-downgrade')
    target.selected_config('compact',elasticsearch_ver='9.5.6')
    retire_upgrade_fixture(target,'compact')
    install(target,'compact',version='9.5.5')
    c=target.selected_config('compact',elasticsearch_requests=[dict(repository,id='rollback-reader-repository')])
    target.product('setup',target.NAME,'07','--tags','elasticsearch',timeout=5400)
    with es.connection(c,k) as request:
        repo=request('GET','/_snapshot/gate-archive')['gate-archive']
        if str(repo['settings'].get('readonly')).lower()!='true':raise ValueError('rollback target repository is not readonly')
        if request('GET','/_snapshot/gate-archive/pre-upgrade')['snapshots'][0]['uuid']!=backup['snapshot_uuid']:raise ValueError('rollback snapshot identity changed')
    target.selected_config('compact',elasticsearch_requests=[{'id':'rollback-restore','type':'restore','repository':'gate-archive','snapshot':'pre-upgrade','indices':[ledger['index']]}])
    target.product('setup',target.NAME,'07','--tags','elasticsearch',timeout=5400)
    verify_documents(target,backup)
    target.selected_config('compact',elasticsearch_requests=[])
    for topology in ('standard','compact'):source.verify_topology(topology);source.data_ledger(topology)
    print('ELASTICSEARCH_CASE_PASS id=ES-52',flush=True)
    print('ELASTICSEARCH_UPGRADE_SCOPED_CLEAN_PASS downgrade=refused old_empty_target=restored source=unchanged',flush=True)


def resume_before_isolation(source, target, c, tc, backup, record):
    """Reuse a proven empty target only after the source is healthy and unfenced."""
    if (record.get('phase') not in ('before-isolation','source-isolating') or record.get('snapshot_uuid')!=backup['snapshot_uuid'] or
            list(SOURCE.glob('fault-*.json')) or (SOURCE/'application-route.json').exists()):
        raise ValueError('DR retry requires its fault-free pre-restore checkpoint')
    if source.api().get('elasticsearch',c['elasticsearch_name'],c['elasticsearch_namespace'])['metadata']['uid']!=record['source_cr_uid']:
        raise ValueError('DR retry source identity changed')
    with source.load_es().connection(tc,target.api()) as request:
        if request('GET','/')['cluster_uuid']!=record['target_cluster_uuid']:
            raise ValueError('DR retry prepared target identity changed')
        if any(not row['index'].startswith('.') for row in request('GET','/_cat/indices?format=json&expand_wildcards=all')):
            raise ValueError('DR retry prepared target is not empty')
    source.verify_topology('compact');target.verify_topology('compact')
    ledger=source.data_ledger('compact')
    if (ledger['index']!=backup['index'] or ledger['documents'].get(record['source_tail_id'])!=record['source_tail'] or
            {key:value for key,value in ledger['documents'].items() if key!=record['source_tail_id']}!=backup['documents']):
        raise ValueError('DR retry source tail or original snapshot ledger changed')


def disaster(source):
    """Fence the source writer before a measured application cutover/replay."""
    target=target_lab();baseline(target)
    backup=json.loads((SOURCE/'backup-compact.json').read_text())
    c,k,es=source.selected_config('compact'),source.api(),source.load_es()
    tc=json.loads((target.STATE/'config-target-compact.json').read_text())
    path=SOURCE/'dr-disaster.json'
    resuming=path.exists()
    if resuming:
        record=json.loads(path.read_text())
        resume_before_isolation(source,target,c,tc,backup,record)
        prepared_uuid=record['target_cluster_uuid']
    else:
        restored=json.loads((target.STATE/'restore-compact.json').read_text())
        if restored['source_snapshot_uuid']!=backup['snapshot_uuid']:raise ValueError('DR source/target snapshot receipt differs')
        verify_documents(target,backup)
        retire_business(target,'compact');install(target,'compact')
        tc=json.loads((target.STATE/'config-target-compact.json').read_text())
        with es.connection(tc,target.api()) as request:prepared_uuid=request('GET','/')['cluster_uuid']
    if prepared_uuid==backup['source_cluster_uuid']:
        raise ValueError('prepared DR cluster reused the source data identity')
    token=json.loads((SOURCE/'ownership.json').read_text())['id'].replace('-','')
    tail_id='source-after-snapshot-'+token
    target_id='target-after-cutover-'+token
    tail={'business_id':tail_id,'value':-11};new={'business_id':target_id,'value':-17}
    expected={'phase':'before-isolation','target_cluster_uuid':prepared_uuid,
            'source_cr_uid':k.get('elasticsearch','search',c['elasticsearch_namespace'])['metadata']['uid'],
            'snapshot_uuid':backup['snapshot_uuid'],'source_tail_id':tail_id,'source_tail':tail,
            'target_write_id':target_id,'target_document':new,'source_snapshot_end_time':backup['end_time_ms']/1000}
    if resuming and any(record.get(key)!=value for key,value in expected.items() if key!='phase'):
        raise ValueError('DR retry operation inputs changed')
    if resuming:
        source.atomic(SOURCE/'dr-disaster-history'/(record['phase']+'-'+secrets.token_hex(8)+'.json'),record)
        print('ELASTICSEARCH_DR_PREPARED_TARGET_REUSED cluster_uuid='+prepared_uuid,flush=True)
    record=expected
    target.atomic(target.STATE/'config-compact.json',tc)
    source.atomic(path,record)
    with es.connection(c,k) as request:
        if resuming:
            current=request('GET','/'+backup['index']+'/_doc/'+tail_id)
            if not current.get('found') or current.get('_source')!=tail:raise ValueError('DR retry source tail identity changed')
            # Reconfirm the same owned document with concurrency protection;
            # measure this real acknowledgement rather than inventing the
            # timestamp missing from an interrupted old receipt.
            suffix='&if_seq_no='+str(current['_seq_no'])+'&if_primary_term='+str(current['_primary_term'])
            write='/_doc/'+tail_id
        else:
            suffix='';write='/_create/'+tail_id
        result=request('PUT','/'+backup['index']+write+'?wait_for_active_shards=all&refresh=wait_for'+suffix,tail)
        if result.get('_shards',{}).get('successful',0)<2 or result['_shards']['failed']:
            raise ValueError('DR source tail lacks confirmed replica acknowledgements')
    tail_ack=time.time();record['tail_ack_time']=tail_ack
    if resuming:record['tail_reconfirmed_for_resume']=True
    source.atomic(path,record)
    ledger=json.loads((SOURCE/'ledger-compact.json').read_text())
    ledger['documents'][tail_id]=tail;source.atomic(SOURCE/'ledger-compact.json',ledger)
    source.data_ledger('compact')
    source.fault_record('statefulset','elastic-operator','elastic-system',1)
    try:
        k.run('-n','elastic-system','scale','statefulset/elastic-operator','--replicas=0')
        k.run('-n','elastic-system','wait','--for=delete','pod/elastic-operator-0','--timeout=120s',timeout=150)
        source.fault_record('statefulset','search-es-data',c['elasticsearch_namespace'],3)
        record['declared_failure_at']=time.time();record['phase']='source-isolating';source.atomic(path,record)
        k.run('-n',c['elasticsearch_namespace'],'scale','statefulset/search-es-data','--replicas=0')
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            pods=[p for p in k.get('pods',namespace=c['elasticsearch_namespace'])['items'] if p['metadata'].get('labels',{}).get('elasticsearch.k8s.elastic.co/cluster-name')=='search']
            endpoints=[endpoint for item in k.get('endpointslices',namespace=c['elasticsearch_namespace'])['items']
                       if item['metadata'].get('labels',{}).get('kubernetes.io/service-name')=='search-client'
                       for endpoint in (item.get('endpoints') or []) if endpoint.get('conditions',{}).get('ready')]
            if not pods and not endpoints:break
            print('ELASTICSEARCH_WAIT_HEARTBEAT dr=source-write-isolation',flush=True);time.sleep(3)
        else:raise ValueError('source still has a live write member or ready client endpoint')
        record['phase']='source-isolated';source.atomic(path,record)
        restore(target,'compact')
        restored=json.loads((target.STATE/'restore-compact.json').read_text())
        if restored['target_cluster_uuid']!=prepared_uuid:raise ValueError('DR restore replaced the prepared target identity')
        target.verify_topology('compact');verify_documents(target,backup)
        with es.connection(tc,target.api()) as request:
            absent=request('POST','/'+backup['index']+'/_mget',{'ids':[tail_id]})['docs']
            if len(absent)!=1 or absent[0].get('found') is not False:raise ValueError('post-snapshot tail unexpectedly exists at restore point')
        # The client below consumes this protected routing input. It changes
        # only after the original source writer is proved stopped.
        source.atomic(SOURCE/'application-route.json',{'cluster':'elasticsearch-dr','namespace':tc['elasticsearch_namespace'],
            'service':tc['elasticsearch_name']+'-client','cluster_uuid':restored['target_cluster_uuid'],'source_isolated':True})
        with routed_connection(source) as request:
            result=request('PUT','/'+backup['index']+'/_create/'+target_id+'?wait_for_active_shards=all',new)
            if result.get('_shards',{}).get('successful',0)<2 or result['_shards']['failed']:
                raise ValueError('target after cutover write lacks replica acknowledgements')
        verify_documents(target,backup,additional={target_id:new})
        record.update(phase='target-serving',target_business_ready_at=time.time(),source_isolation='zero ES Pods and zero ready client endpoints')
        record['rto_seconds']=record['target_business_ready_at']-record['declared_failure_at']
        record['rpo_seconds']=max(0,tail_ack-record['source_snapshot_end_time'])
        record['confirmed_source_writes_outside_snapshot']=1
        source.atomic(path,record)
        # Stop the fixture writer and block the exact target index before the
        # old source can return. Replay is explicit stable-ID business work.
        with es.connection(tc,target.api()) as request:
            request('PUT','/'+backup['index']+'/_settings',{'index.blocks.write':True})
            observed=request('GET','/'+backup['index']+'/_settings?flat_settings=true')[backup['index']]['settings']
            if str(observed.get('index.blocks.write')).lower()!='true':raise ValueError('target writer was not blocked before failback')
        record['phase']='target-write-blocked';source.atomic(path,record)
        source.restore_faults()
        source.verify_topology('compact');source.data_ledger('compact')
        with es.connection(c,k) as request:
            existing=request('POST','/'+backup['index']+'/_mget',{'ids':[target_id]})['docs'][0]
            if existing.get('found') and existing.get('_source')!=new:raise ValueError('target write conflicts with source stable ID; preserve both and stop failback')
            if not existing.get('found'):
                result=request('PUT','/'+backup['index']+'/_create/'+target_id+'?wait_for_active_shards=all&refresh=wait_for',new)
                if result.get('_shards',{}).get('successful',0)<2 or result['_shards']['failed']:raise ValueError('failback replay lacks replica acknowledgements')
        ledger=json.loads((SOURCE/'ledger-compact.json').read_text());ledger['documents'][target_id]=new
        source.atomic(SOURCE/'ledger-compact.json',ledger);source.data_ledger('compact')
        source.atomic(SOURCE/'application-route.json',{'cluster':source.NAME,'namespace':c['elasticsearch_namespace'],
            'service':c['elasticsearch_name']+'-client','cluster_uuid':backup['source_cluster_uuid'],'target_write_blocked':True})
        with routed_connection(source) as request:
            result=request('POST','/'+backup['index']+'/_mget',{'ids':[tail_id,target_id]})['docs']
            if {row['_id']:row['_source'] for row in result if row.get('found')}!={tail_id:tail,target_id:new}:
                raise ValueError('restored application route does not return both source tail and replayed target write')
        record.update(phase='complete',failback='original source tail retained; target stable-ID write replayed; target remains write-blocked')
        source.atomic(path,record)
    finally:
        primary=sys.exc_info()[1]
        try:
            # Never restore source writers while the target is still writable.
            fence_target_writer(source)
            source.restore_faults()
        except Exception as error:
            if primary is None:raise
            print('ELASTICSEARCH_DR_RECOVERY_FAIL reason='+type(error).__name__,file=sys.stderr)
    for topology in ('standard','compact'):source.verify_topology(topology);source.data_ledger(topology)
    print('ELASTICSEARCH_DR_MEASUREMENT '+json.dumps(dict({key:record[key] for key in ('rto_seconds','rpo_seconds','confirmed_source_writes_outside_snapshot','failback')},
        conditions='independent K8s/CSI/empty target prepared before declaration; RTO includes restore, query and replicated target write; RPO is confirmed-tail-to-snapshot-end interval, not cross-shard instantaneous consistency'),sort_keys=True),flush=True)
    for number in (43,44,45):print('ELASTICSEARCH_CASE_PASS id=ES-'+str(number),flush=True)
    print('ELASTICSEARCH_DR_SCOPED_CLEAN_PASS source=serving target=write-blocked replay=verified',flush=True)


def fence_target_writer(source):
    path=SOURCE/'dr-disaster.json'
    if not path.exists():return
    record=json.loads(path.read_text())
    # Complete is written only after target fencing, source replay and route
    # readback. Later isolated restore exercises may retire that old target.
    if record['phase'] in ('before-isolation','source-isolating','complete'):return
    target=target_lab();baseline(target)
    c=json.loads((target.STATE/'config-target-compact.json').read_text())
    backup=json.loads((SOURCE/'backup-compact.json').read_text())
    with source.load_es().connection(c,target.api()) as request:
        info=request('GET','/')
        if not record.get('target_cluster_uuid') or info['cluster_uuid']!=record['target_cluster_uuid']:
            raise ValueError('DR source recovery refuses changed target identity')
        # The target may have no restored index when restore failed before
        # acceptance. Its exact fixture route was never opened in that case.
        indices=request('GET','/'+backup['index']+'/_settings?ignore_unavailable=true')
        if indices:
            request('PUT','/'+backup['index']+'/_settings',{'index.blocks.write':True})
            state=request('GET','/'+backup['index']+'/_settings?flat_settings=true')[backup['index']]['settings']
            if str(state.get('index.blocks.write')).lower()!='true':raise ValueError('DR recovery cannot prove target writer isolation')


@contextmanager
def routed_connection(source):
    route=json.loads((SOURCE/'application-route.json').read_text())
    if route['cluster']=='elasticsearch-dr':
        if route.get('source_isolated') is not True:raise ValueError('application route lacks source write isolation')
        lab=target_lab();baseline(lab)
        c=json.loads((lab.STATE/'config-target-compact.json').read_text())
    elif route['cluster']==source.NAME:
        if route.get('target_write_blocked') is not True:raise ValueError('application failback lacks target write isolation')
        lab=source;c=source.selected_config('compact')
    else:raise ValueError('application route names an unowned cluster')
    if route['namespace']!=c['elasticsearch_namespace'] or route['service']!=c['elasticsearch_name']+'-client':
        raise ValueError('application route service identity differs')
    with source.load_es().connection(c,lab.api()) as request:
        if request('GET','/')['cluster_uuid']!=route['cluster_uuid']:raise ValueError('application connected to a different data cluster')
        yield request


def cleanup(lab):
    if (lab.STATE/'ownership.json').exists(): baseline(lab) if (lab.STATE/'baseline.json').exists() else None
    for topology in ('standard','compact'):
        path=lab.STATE/('config-target-'+topology+'.json')
        if path.exists():lab.atomic(lab.STATE/('config-'+topology+'.json'),json.loads(path.read_text()))
    lab.cleanup()
    if lab.DIRECTORY.exists() or (lab.STATE/'ownership.json').exists(): raise ValueError('DR owned cluster cleanup incomplete')
    print('ELASTICSEARCH_DR_CLEAN_PASS source_backend=preserved raw_disks=untouched',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('admission','bootstrap','install','restore','cleanup'))
    p.add_argument('--topology',choices=('standard','compact'),default='compact')
    args=p.parse_args()
    if os.environ.get('ELASTICSEARCH_PARENT_LEASE_HELD')!='yes': raise ValueError('both fixed parent pool leases are required')
    lab=target_lab();lab.STATE.mkdir(parents=True,mode=0o700,exist_ok=True)
    if args.action in ('install','restore'): globals()[args.action](lab,args.topology)
    else: globals()[args.action](lab)


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('ELASTICSEARCH_DR_REFUSED reason='+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
