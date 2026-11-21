"""Supply only missing fixed lab images from already accepted official mirrors.

Existing lab tags are never overwritten. Legacy Docker manifests may differ in
serialization/build metadata: require identical rootfs diff_ids, runtime config,
architecture and OS before reuse. This is fixture identity, not a new release.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
LAB='http://192.168.122.2:5000'
HUB='https://hub.talkedu.cn'
ACCEPT=','.join(['application/vnd.oci.image.index.v1+json','application/vnd.docker.distribution.manifest.list.v2+json',
                 'application/vnd.oci.image.manifest.v1+json','application/vnd.docker.distribution.manifest.v2+json'])


def scope(*, ingress=False):
    records=ROOT/'upgrade/v1.35.10/evidence'
    observed=json.loads((records/'consumer-readback-plan.json').read_text())['observed']
    observed+=json.loads((records/'source-image-readback.json').read_text())['plan']['observed']
    names=['brinnatt/pause:3.10','brinnatt/busybox:1.37','brinnatt/local-path-provisioner:v0.0.37']
    names += ['brinnatt/'+name+':'+version for version in ['v3.32.4']
              for name in ['calico-node','calico-cni','calico-kube-controllers']]
    names += ['brinnatt/'+name+':'+version for name,versions in [
        ('coredns',['1.15.0']),('k8s-dns-node-cache',['1.26.8']),
        ('metrics-server',['v0.9.1'])] for version in versions]
    lookup={i['row']['image']+':'+i['row']['tag']:i for i in observed}
    if ingress:
        observed=json.loads((ROOT/'upgrade/v1.36.6/evidence/consumer-readback-plan.json').read_text())['observed']
        lookup={i['row']['image']+':'+i['row']['tag']:i for i in observed}
        names=['brinnatt/ingress-nginx-controller:v1.15.1','brinnatt/kube-webhook-certgen:v1.6.9']
    return {name:lookup[name]['talkedu'] for name in names}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


@contextmanager
def lab_tunnel():
    """Reach only the fixed lab registry; private credentials stay local."""
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
    command=['ssh','-T','-N','-o','BatchMode=yes','-o','ConnectTimeout=10',
             '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=10',
             '-o','ServerAliveCountMax=3','-J','root@192.168.47.130',
             '-L',f'127.0.0.1:{port}:127.0.0.1:5000','root@192.168.122.2']
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=errors)
        try:
            deadline=time.monotonic()+15
            while True:
                if process.poll() is not None:
                    raise RuntimeError('fixed lab registry tunnel failed before readiness')
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=0.2):break
                except OSError:
                    if time.monotonic()>=deadline:
                        raise RuntimeError('fixed lab registry tunnel readiness timed out') from None
                    time.sleep(0.1)

            class Connection(http.client.HTTPConnection):
                def connect(self):
                    self.sock=socket.create_connection(('127.0.0.1',port),self.timeout)

            class Handler(urllib.request.HTTPHandler):
                def http_open(self,request):
                    if request.host!='192.168.122.2:5000':
                        raise ValueError('tunnel request escaped the fixed lab registry')
                    return self.do_open(Connection,request)

            # Keep the actual registry URL/Host and upload Location validation.
            # Only the TCP connection changes; environment proxies are disabled.
            yield urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect,Handler)
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)


class Registry:
    def __init__(self,local_opener=None):
        path=Path('/home/brinnatt/.hub.talkedu.cn')
        if path.stat().st_mode&0o777!=0o600:raise ValueError('private registry credentials required')
        credentials={}
        for line in path.read_text().splitlines():
            match=re.match(r'\s*([^:=\s]+)\s*[:=]\s*(.*?)\s*$',line)
            if match:credentials[match[1].lower()]=match[2].strip('\"\'')
        user=credentials.get('user',credentials.get('username'))
        password=credentials.get('password',credentials.get('pass'))
        self.basic='Basic '+base64.b64encode((user+':'+password).encode()).decode()
        self.tokens={};self.opener=urllib.request.build_opener(NoRedirect)
        self.local_opener=local_opener or self.opener

    def get(self, repository, kind, reference, *, local=False, optional=False):
        base=LAB if local else HUB
        repository=repository if local else repository.replace('brinnatt/','kubeauto/',1)
        url=base+'/v2/'+repository+'/'+kind+'/'+reference
        for attempt in range(2):
            headers={'Accept':ACCEPT,'Accept-Encoding':'identity'}
            if not local:headers['Authorization']=self.tokens.get(repository,self.basic)
            try:return (self.local_opener if local else self.opener).open(
                urllib.request.Request(url,headers=headers),timeout=30)
            except urllib.error.HTTPError as error:
                if error.code==404 and optional:return None
                if error.code==401 and not local:
                    fields=dict(re.findall(r'(\w+)="([^"]*)"',error.headers.get('WWW-Authenticate','')))
                    realm=fields['realm']
                    if urllib.parse.urlsplit(realm).hostname!='hub.talkedu.cn' or not realm.startswith('https://'):
                        raise ValueError('unexpected registry token authority') from None
                    request=urllib.request.Request(realm+'?'+urllib.parse.urlencode({'service':fields.get('service',''),
                        'scope':'repository:'+repository+':pull'}),headers={'Authorization':self.basic})
                    with self.opener.open(request,timeout=30) as response:body=json.load(response)
                    self.tokens[repository]='Bearer '+body.get('token',body.get('access_token'))
                    continue
                if error.code in [302,307] and not local:
                    location=error.headers['Location']
                    if not location.startswith('https://'):raise ValueError('unsafe blob redirect') from None
                    # Signed blob URLs receive no credentials.
                    try:return urllib.request.urlopen(location,timeout=30)
                    except (urllib.error.URLError,TimeoutError):
                        raise RuntimeError('signed image blob retrieval failed') from None
                raise RuntimeError('registry read HTTP '+str(error.code)) from None
        raise RuntimeError('registry authentication did not converge')

    def document(self,repository,kind,reference,**kwargs):
        response=self.get(repository,kind,reference,**kwargs)
        if response is None:return None
        with response:raw=response.read()
        digest='sha256:'+hashlib.sha256(raw).hexdigest()
        if reference.startswith('sha256:') and reference!=digest:raise ValueError('registry content checksum differs')
        return json.loads(raw),raw,digest

    def write(self,url,method,body=None,content_type=None):
        if urllib.parse.urlsplit(url).netloc!='192.168.122.2:5000' or not url.startswith(LAB+'/v2/brinnatt/'):
            raise ValueError('fixture publication escaped the fixed lab registry')
        headers={}
        if content_type:headers['Content-Type']=content_type
        if hasattr(body,'seek'):
            position=body.tell();body.seek(0,2);headers['Content-Length']=str(body.tell()-position);body.seek(position)
        return self.local_opener.open(urllib.request.Request(url,data=body,method=method,headers=headers),timeout=120)

    def blob(self,repository,descriptor):
        digest=descriptor['digest']
        existing=self.get(repository,'blobs',digest,local=True,optional=True)
        if existing is not None:
            existing.close();return
        with self.get(repository,'blobs',digest) as response,tempfile.TemporaryFile() as spool:
            identity=hashlib.sha256();size=0
            while block:=response.read(1024*1024):
                spool.write(block);identity.update(block);size+=len(block)
            if size!=descriptor['size'] or 'sha256:'+identity.hexdigest()!=digest:
                raise ValueError('image blob checksum differs')
            spool.seek(0)
            with self.write(LAB+'/v2/'+repository+'/blobs/uploads/','POST',b'') as upload:
                location=urllib.parse.urljoin(LAB,upload.headers['Location'])
            separator='&' if '?' in location else '?'
            # Request body is a verified seekable stream; no whole-layer RAM copy.
            with self.write(location+separator+urllib.parse.urlencode({'digest':digest}),'PUT',spool,
                            'application/octet-stream') as result:
                if result.status!=201:raise ValueError('blob upload did not finalize')


def equivalent(expected,actual):
    # Ignore build timestamps/history/labels only. All effective container
    # settings, rootfs diff_ids and platform remain exact.
    def runtime(value):
        config=dict(value.get('config',{}));config.pop('Labels',None)
        return {key:value.get(key) for key in ['architecture','os','variant','rootfs']} | {'config':config}
    return runtime(expected)==runtime(actual)


def execute(registry,publish=False,*,accepted_scope=None):
    records=[]
    for name,accepted in (scope() if accepted_scope is None else accepted_scope).items():
        repository,tag=name.split(':')
        source,raw,digest=registry.document(repository,'manifests',accepted['platforms']['linux/amd64'])
        existing=registry.document(repository,'manifests',tag,local=True,optional=True)
        if existing:
            actual=existing[0]
            if 'manifests' in actual:
                child=next(m for m in actual['manifests'] if m.get('platform',{}).get('architecture')=='amd64')
                actual=registry.document(repository,'manifests',child['digest'],local=True)[0]
            original_config=registry.document(repository,'blobs',source['config']['digest'])[0]
            local_config=registry.document(repository,'blobs',actual['config']['digest'],local=True)[0]
            if not equivalent(original_config,local_config):
                raise ValueError('existing lab tag has different runtime/rootfs; do not overwrite: '+name)
            action='reuse-equivalent-runtime-and-rootfs'
        else:
            action='missing'
            if publish:
                print('NEXT_LAB_IMAGE_COPY_BEGIN image='+name,flush=True)
                for descriptor in [source['config'],*source['layers']]:registry.blob(repository,descriptor)
                if registry.document(repository,'manifests',tag,local=True,optional=True) is not None:
                    raise ValueError('lab tag appeared during supply; refuse overwrite')
                with registry.write(LAB+'/v2/'+repository+'/manifests/'+tag,'PUT',raw,source['mediaType']) as response:
                    if response.status!=201:raise ValueError('manifest publication did not complete')
                if registry.document(repository,'manifests',tag,local=True)[2]!=digest:
                    raise ValueError('lab manifest readback differs')
                action='copied-missing-amd64-manifest'
        records.append({'image':name,'source_child':digest,'action':action})
        print('NEXT_LAB_IMAGE image='+name+' action='+action,flush=True)
    return {'schema_version':1,'registry':LAB,'builds':[],'existing_tags_overwritten':[],
            'records':records,'ready':all(r['action']!='missing' for r in records)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--supply-missing',action='store_true')
    parser.add_argument('--ingress',action='store_true',help='Only the two frozen ingress qualification images')
    args=parser.parse_args()
    with lab_tunnel() as local_opener:
        result=execute(Registry(local_opener),args.supply_missing,accepted_scope=scope(ingress=args.ingress))
    filename='ingress-registry-supply.json' if args.ingress else 'lab-registry-supply.json'
    (ROOT/'upgrade/v1.36.6/evidence'/filename).write_text(json.dumps(result,indent=2)+'\n')
    if result['ready']:print('NEXT_LAB_REGISTRY_QUALIFIED_PASS',flush=True)
