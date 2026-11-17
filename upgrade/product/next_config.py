"""Preserve the original config and publish only fields actually advanced by this hop."""
import os
from pathlib import Path
import re
import tempfile
import yaml

from upgrade.product.next_platform import snapshot
from upgrade.product.kubernetes_preflight import Refused


def configuration(path, state):
    saved=snapshot(state/'product-config.json',lambda:{'text':path.read_text()})
    original=yaml.safe_load(saved['text'])
    if not isinstance(original,dict):raise Refused('source product config must be a mapping')
    updates={'K8S_VER':'1.35.10','calico_ver':'v3.32.4'}
    if original.get('CONTAINER_RUNTIME')=='containerd':updates['CONTAINERD_CONFIG_VERSION']=3
    if original.get('dns_install')=='yes':
        updates['corednsVer']='1.15.0'
        if original.get('ENABLE_LOCAL_DNS_CACHE') is True:updates['dnsNodeCacheVer']='1.26.8'
    if original.get('metricsserver_install')=='yes':updates['metricsVer']='v0.9.1'
    desired=dict(original,**updates)
    current=yaml.safe_load(path.read_text())
    if current not in [original,desired]:raise Refused('product config drifted outside the admitted source or final state')
    text=saved['text']
    for key,value in updates.items():
        line=key+': '+str(value)
        pattern=r'^'+re.escape(key)+r':[^\n]*$'
        text=re.sub(pattern,lambda match:line,text,flags=re.MULTILINE) if re.search(pattern,text,re.MULTILINE) else text.rstrip()+'\n'+line+'\n'
    return original,text


def publish(path,text):
    if path.read_text()==text:return
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as stream:
        temporary=Path(stream.name)
        try:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        finally:temporary.unlink(missing_ok=True)
