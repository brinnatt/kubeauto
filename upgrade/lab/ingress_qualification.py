"""Focused target ingress qualification at the owned new-install handoff.

Uses config + kubecli setup 07, keeps the frozen product configuration unchanged,
and removes only newly owned Helm/fixture objects and its exact node labels.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import yaml

from upgrade.lab.next_session import Session, STATE, ROOT, MASTERS, WORKERS, sha, write
from upgrade.product.launcher import admit_static
from upgrade.product.hop import MATRIX_PATH
from upgrade.validate import check_hash

NAMESPACE='ingress-nginx'
FIXTURE='upgrade-ingress-probe'
HOST='upgrade-ingress.invalid'


def boundary(session):
    scope=yaml.safe_load((MATRIX_PATH.parent/'acceptance-plan.yaml').read_text()).get('ingress_qualification',{})
    if scope!={'enabled':True,'runner':'upgrade/lab/ingress_qualification.py','phase':'new-install',
               'images':['brinnatt/ingress-nginx-controller:v1.15.1','brinnatt/kube-webhook-certgen:v1.6.9']}:
        raise ValueError('focused ingress scope must be explicitly frozen')
    static=json.loads((MATRIX_PATH.parent/'evidence/ingress-focused-contracts.json').read_text())
    if static.get('passed') is not True or not static.get('inputs') or not static.get('references'):
        raise ValueError('current ingress static qualification required')
    for name,digest in static['inputs'].items():check_hash(ROOT,name,digest)
    for ref in static['references']:check_hash(ROOT,ref['path'],ref['sha256'],evidence=True)
    owner=json.loads((STATE/'owner.json').read_text())
    if owner.get('run_id')!=session.run_id or owner.get('mode') not in ['qualify','final']:
        raise ValueError('qualification owner differs')
    status=json.loads((session.evidence/'new-install.status.json').read_text())
    if status.get('rc')!=0 or status.get('run_id')!=session.run_id:
        raise ValueError('healthy new-install handoff required')
    attempt=status['attempt']
    for suffix in ['exit','finalized']:
        if (session.evidence/f'new-install.attempt-{attempt}.{suffix}').read_text().strip()!='0':
            raise ValueError('durable installation result required')
    if list(session.evidence.glob('target-clean.attempt-*')):
        raise ValueError('target cleanup already started')
    lines=(STATE/(session.run_id+'.log')).read_text().splitlines()
    waits=[line for line in lines if line.startswith('NEXT_SESSION_WAIT ')]
    if owner['mode']=='qualify' and (not waits or waits[-1]!='NEXT_SESSION_WAIT phase=new-install failed=false lease=held'):
        raise ValueError('session must retain its lease at the new-install boundary')
    session.select(target=True)
    config=json.loads((session.evidence/'target-config.json').read_text())
    if config['sha256']!=sha(config['path']):raise ValueError('frozen target configuration changed')
    session.host_operation('marked')
    return config


def attempt_prefix(attempt):
    if attempt not in [1,2,3]:raise ValueError('bounded ingress attempt required')
    return 'ingress' if attempt==1 else 'ingress-attempt-'+str(attempt)


def retry_boundary(session,attempt):
    if attempt==1:return
    previous=attempt_prefix(attempt-1)
    for suffix in ['exit','finalized']:
        if (session.evidence/(previous+'.'+suffix)).read_text().strip()!='1':
            raise ValueError('retry requires the prior durable failed result')
    if 'UPGRADE_INGRESS_SCOPED_CLEAN_VERIFY_PASS' not in (session.evidence/(previous+'.log')).read_text():
        raise ValueError('retry requires verified prior scoped cleanup')


def replace_configuration(path,content):
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.ingress-config-',delete=False) as stream:
            temporary=Path(stream.name)
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


class Probe:
    def __init__(self,session,attempt=1):
        self.session=session
        self.directory=session.evidence/('ingress-qualification' if attempt==1 else 'ingress-qualification-attempt-'+str(attempt))
        self.directory.mkdir(mode=0o700,exist_ok=False)
        self.namespaces={};self.labels={};self.installed=False

    def kube(self,*args,data=None):
        return self.session.kubectl(*args,data=data)

    def apply(self,value):
        return self.kube('apply','-f','-',data=json.dumps(value))

    def initialize(self):
        for resource in ['namespace/'+NAMESPACE,'namespace/'+FIXTURE,'ingressclass/nginx',
                         'validatingwebhookconfiguration/ingress-nginx-admission',
                         'clusterrole/ingress-nginx','clusterrole/ingress-nginx-admission',
                         'clusterrolebinding/ingress-nginx','clusterrolebinding/ingress-nginx-admission']:
            if self.kube('get',resource,'--ignore-not-found','-o','name').strip():
                raise ValueError('foreign ingress object prevents qualification: '+resource)
        nodes=json.loads(self.kube('get','nodes','-o','json'))['items']
        for node in nodes:
            key='ingress-controller/provider'
            self.labels[node['metadata']['name']]={'uid':node['metadata']['uid'],
                'original':node['metadata'].get('labels',{}).get(key)}
        for name in [NAMESPACE,FIXTURE]:
            obj=json.loads(self.kube('create','namespace',name,'-o','json'))
            self.namespaces[name]=obj['metadata']['uid']
        write(self.directory/'ownership.json',{'namespaces':self.namespaces,'nodes':self.labels})

    def install(self):
        # setup's legacy cmdline joins argv and appends config extra-vars last.
        # Use the normal product config path, with an owned temporary overlay,
        # then restore the exact frozen bytes before any business assertion.
        path=self.session.target/'clusters'/self.session.cluster/'config.yml'
        original=path.read_bytes()
        backup=self.directory/'frozen-config.yml'
        with backup.open('xb') as stream:stream.write(original)
        backup.chmod(0o600)
        configuration=yaml.safe_load(original)
        configuration.update(ingress_nginx_install='yes',ingress_nginx_metrics_enabled=False,
                             ingress_nginx_namespace=NAMESPACE)
        overlay=yaml.safe_dump(configuration,allow_unicode=True,sort_keys=False).encode()
        self.installed=True
        replace_configuration(path,overlay)
        try:
            self.session.cli(self.session.target,self.session.target,'setup',self.session.cluster,'07')
        finally:
            if path.read_bytes()!=overlay:raise ValueError('configuration changed outside the owned ingress overlay; retain backup')
            replace_configuration(path,original)
        write(self.directory/'configuration-restored.json',{'path':str(path),'sha256':sha(path),
            'fields':['ingress_nginx_install','ingress_nginx_metrics_enabled','ingress_nginx_namespace']})

    def exercise(self):
        cert,key=self.directory/'tls.pem',self.directory/'tls.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
            '-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN='+HOST,
            '-addext','subjectAltName=DNS:'+HOST],capture_output=True,check=True,timeout=30)
        key.chmod(0o600)
        self.kube('-n',FIXTURE,'create','secret','tls','probe','--cert='+str(cert),'--key='+str(key))
        image='registry.talkschool.cn:5000/brinnatt/busybox:1.37'
        pod={'containers':[{'name':'http','image':image,'command':['sh','-ec',
             'mkdir -p /www; printf "%s\\n" "'+self.session.run_id+'" > /www/confirmed; exec httpd -f -p 8080 -h /www'],
             'ports':[{'containerPort':8080}]}]}
        self.apply({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'backend','namespace':FIXTURE},
            'spec':{'replicas':1,'selector':{'matchLabels':{'app':'upgrade-ingress'}},
                    'template':{'metadata':{'labels':{'app':'upgrade-ingress'}},'spec':pod}}})
        self.apply({'apiVersion':'v1','kind':'Service','metadata':{'name':'backend','namespace':FIXTURE},
            'spec':{'selector':{'app':'upgrade-ingress'},'ports':[{'port':8080,'targetPort':8080}]}})
        self.kube('-n',FIXTURE,'rollout','status','deployment/backend','--timeout=180s')
        ingress={'apiVersion':'networking.k8s.io/v1','kind':'Ingress',
            'metadata':{'name':'confirmed','namespace':FIXTURE,
                        'annotations':{'nginx.ingress.kubernetes.io/ssl-redirect':'false'}},
            'spec':{'ingressClassName':'nginx','tls':[{'hosts':[HOST],'secretName':'probe'}],
             'rules':[{'host':HOST,'http':{'paths':[{'path':'/','pathType':'Prefix',
               'backend':{'service':{'name':'backend','port':{'number':8080}}}}]}}]}}
        self.apply(ingress)
        invalid=json.loads(json.dumps(ingress));invalid['metadata']['name']='duplicate'
        command=[str(self.session.target/'kube-bin/kubectl'),'--kubeconfig',
            str(self.session.target/'clusters'/self.session.cluster/'kubectl.kubeconfig'),
            'create','--dry-run=server','-f','-']
        refusal=subprocess.run(command,input=json.dumps(invalid),text=True,capture_output=True,timeout=30)
        (self.directory/'admission-refusal.log').write_text(refusal.stdout+refusal.stderr)
        if not refusal.returncode or 'admission webhook' not in refusal.stderr or 'already defined' not in refusal.stderr:
            raise ValueError('duplicate ingress was not refused by the real admission webhook')
        pods=json.loads(self.kube('-n',NAMESPACE,'get','pods','-l',
            'app.kubernetes.io/component=controller','-o','json'))['items']
        ready=[p for p in pods if any(c['type']=='Ready' and c['status']=='True' for c in p['status'].get('conditions',[]))]
        if not ready:raise ValueError('no Ready ingress controller')
        node=json.loads(self.kube('get','node',ready[0]['spec']['nodeName'],'-o','json'))
        address=next(a['address'] for a in node['status']['addresses'] if a['type']=='InternalIP')
        if address not in MASTERS+WORKERS:raise ValueError('ingress endpoint escaped leased pool')
        svc=json.loads(self.kube('-n',NAMESPACE,'get','service','ingress-nginx-controller','-o','json'))
        ports={p['name']:p['nodePort'] for p in svc['spec']['ports']}
        for scheme in ['http','https']:
            port=ports[scheme]
            args=['curl','--noproxy','*','--silent','--show-error','--fail','--connect-timeout','3',
                  '--max-time','10','--resolve',f'{HOST}:{port}:{address}']
            if scheme=='https':args+=['--cacert',str(cert)]
            deadline=time.monotonic()+90
            while True:
                result=subprocess.run([*args,f'{scheme}://{HOST}:{port}/confirmed'],capture_output=True,timeout=15)
                if result.returncode==0 and result.stdout==(self.session.run_id+'\n').encode():break
                if time.monotonic()>=deadline:raise ValueError('verified ingress route did not return the confirmed payload')
                time.sleep(3)
        wrong=subprocess.run(['curl','--noproxy','*','--silent','--show-error','--max-time','10',
            '--cacert',str(self.session.target/'clusters'/self.session.cluster/'ssl/ca.pem'),
            '--resolve',f'{HOST}:{ports["https"]}:{address}',f'https://{HOST}:{ports["https"]}/confirmed'],capture_output=True,timeout=15)
        if wrong.returncode!=60:raise ValueError('wrong CA did not produce a TLS verification refusal')
        write(self.directory/'result.json',{'run_id':self.session.run_id,'http_payload':True,'verified_https_payload':True,
            'wrong_ca_rc':wrong.returncode,'admission_refusal_rc':refusal.returncode,'endpoint':address,
            'boundary':'project integration on 1.36.6, not upstream per-version certification'})
        print('UPGRADE_INGRESS_ROUTES_TLS_ADMISSION_PASS',flush=True)

    def clean(self):
        if not self.namespaces:
            return  # Initial foreign-object refusal has no owned mutation.
        for name,uid in self.namespaces.items():
            actual=json.loads(self.kube('get','namespace',name,'-o','json'))['metadata']['uid']
            if actual!=uid:raise ValueError('namespace ownership changed; refuse cleanup')
        if self.installed:
            self.session.command([str(self.session.target/'extra-bin/helm'),'uninstall','ingress-nginx',
                '-n',NAMESPACE,'--ignore-not-found','--kubeconfig',
                str(self.session.target/'clusters'/self.session.cluster/'kubectl.kubeconfig')])
        for name,uid in self.namespaces.items():
            self.kube('delete','--raw=/api/v1/namespaces/'+name,'-f','-',data=json.dumps(
                {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid}}))
            self.kube('wait','--for=delete','namespace/'+name,'--timeout=180s')
        for name,record in self.labels.items():
            node=json.loads(self.kube('get','node',name,'-o','json'))
            if node['metadata']['uid']!=record['uid']:raise ValueError('node ownership changed')
            value=node['metadata'].get('labels',{}).get('ingress-controller/provider')
            if value==record['original']:continue
            if value!='ingress-nginx':raise ValueError('node label changed outside qualification')
            operations=[{'op':'test','path':'/metadata/uid','value':record['uid']},
                {'op':'test','path':'/metadata/labels/ingress-controller~1provider','value':'ingress-nginx'}]
            operations.append({'op':'remove','path':'/metadata/labels/ingress-controller~1provider'} if record['original'] is None
                else {'op':'replace','path':'/metadata/labels/ingress-controller~1provider','value':record['original']})
            self.kube('patch','node',name,'--type=json','-p',json.dumps(operations))
        for resource in ['namespace/'+NAMESPACE,'namespace/'+FIXTURE,'ingressclass/nginx',
                         'validatingwebhookconfiguration/ingress-nginx-admission',
                         'clusterrole/ingress-nginx','clusterrole/ingress-nginx-admission',
                         'clusterrolebinding/ingress-nginx','clusterrolebinding/ingress-nginx-admission']:
            if self.kube('get',resource,'--ignore-not-found','-o','name').strip():
                raise ValueError('owned ingress resources remain after cleanup')
        print('UPGRADE_INGRESS_SCOPED_CLEAN_VERIFY_PASS',flush=True)


def run(run_id,attempt=1):
    session=Session('qualify',run_id)
    run_session(session,attempt)


def run_session(session,attempt=1):
    admit_static(MATRIX_PATH,required=['UPG-01','UPG-02','UPG-03','UPG-04'])
    config=boundary(session)
    retry_boundary(session,attempt)
    probe=Probe(session,attempt)
    def interrupted(signum,frame):raise InterruptedError('ingress qualification interrupted')
    previous=signal.signal(signal.SIGTERM,interrupted)
    try:
        probe.initialize();probe.install();probe.exercise()
    except BaseException as error:
        write(probe.directory/'first-failure.json',{'reason':str(error)})
        try:probe.clean()
        except Exception as cleanup_error:
            write(probe.directory/'cleanup-failure.json',{'reason':str(cleanup_error)})
        raise
    else:
        probe.clean()
    finally:
        signal.signal(signal.SIGTERM,previous)
        if sha(config['path'])!=config['sha256']:raise ValueError('qualification changed the frozen config')
    print('UPGRADE_INGRESS_QUALIFICATION_PASS',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--attempt',type=int,choices=[1,2,3],default=1)
    args=parser.parse_args()
    run(args.run_id,args.attempt)
