#!/usr/bin/env python3
"""Leased ES fixture orchestration. Product behavior always enters kubecli.

This module only operates the explicit disposable source pool. It does not
format disks, borrow the current kubeconfig, or alter the control host's AIO.
"""
import argparse
import base64
import fcntl
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import inspect
import ipaddress
import re
import uuid
from pathlib import Path
import secrets
import shlex
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import urlopen

import yaml

BASE = Path('/usr/local/kubeauto')
NAME = 'elasticsearch-gate'
DIRECTORY = BASE / 'clusters' / NAME
STATE = Path('/var/tmp/kubeauto-elasticsearch')
PREVIOUS_HOSTS = ('192.168.122.243', '192.168.122.246', '192.168.122.217',
                  '192.168.122.210', '192.168.122.216', '192.168.122.193')
SPARE_HOSTS = ('192.168.122.212',)
HOSTS = PREVIOUS_HOSTS + SPARE_HOSTS
NODES = tuple('es-master-' + ip.rsplit('.', 1)[1] if i < 3 else 'es-data-' + ip.rsplit('.', 1)[1] for i, ip in enumerate(HOSTS))
LABELS = {'app.kubernetes.io/managed-by': 'kubeauto', 'kubeauto.io/test-owner': 'elasticsearch-gate'}


def execute(argv, *, capture=True, body=None, timeout=180):
    result = subprocess.run([str(a) for a in argv], input=body, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout)
    if result.returncode:
        # Do not relay arbitrary response bodies or shell stderr with secrets.
        raise ValueError('fixture command failed executable=' + Path(str(argv[0])).name + ' rc=' + str(result.returncode))
    return result.stdout if capture else ''


def product(*args, timeout=3600):
    if args and args[0]=='download':
        if '-E' in args:stage_images(args[args.index('-E')+1])
        elif '-D' in args:stage_images('platform-default')
    execute(['kubecli', *args], capture=False, timeout=timeout)


def stage_images(group):
    """Freeze only this consumer's local cache before kubecli may reuse it."""
    import docker
    import shutil
    from common.constants import KubeConstant
    spec=importlib.util.spec_from_file_location('es_image_records',BASE/'tests/helpers/elasticsearch-artifacts.py')
    artifacts=importlib.util.module_from_spec(spec);spec.loader.exec_module(artifacts)
    rows=artifacts.records(json.loads((BASE/'tests/evidence/elasticsearch-images-20261103.json').read_text()))
    if group=='platform-default':
        names={'calico-cni','calico-node','calico-kube-controllers','coredns','k8s-dns-node-cache','metrics-server','pause','ansible'}
        keys={key for key in rows if key.removeprefix('brinnatt/').split(':')[0] in names}
    elif group=='snapshot-fixtures':
        keys={'brinnatt/minio:RELEASE.2025-04-08T15-41-24Z','brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z'}
    else:keys=set(KubeConstant().component_images[group])
    if not keys or not keys<=set(rows):raise ValueError('consumer image group escapes the frozen publication')
    client=docker.from_env()
    try:
        root=Path(client.info()['DockerRootDir'])
        for key in sorted(keys):
            row=rows[key];ref=row['talkedu_ref'].rsplit(':',1)[0]+'@'+row['digest']
            try:image=client.images.get(ref)
            except docker.errors.ImageNotFound:
                if shutil.disk_usage(root).free<4*1024**3:raise ValueError('control image cache lacks 4 GiB free headroom; preserve unrelated artifacts')
                print('ELASTICSEARCH_IMAGE_STAGING image='+key,flush=True)
                image=client.images.pull(ref)
            if ref not in image.attrs.get('RepoDigests',[]):raise ValueError('local fixture image lacks its frozen publication digest')
            repository,tag=key.rsplit(':',1)
            if not image.tag(repository,tag):raise ValueError('cannot bind local product image tag to the frozen content')
            print('ELASTICSEARCH_IMAGE_STAGED image='+key+' digest='+row['digest'],flush=True)
    finally:client.close()


def product_refusal(reason, label, *, tags='elasticsearch', marker='ELASTICSEARCH_REFUSED'):
    """An expected refusal needs the product's specific cause, not any rc!=0."""
    result = subprocess.run(['kubecli', 'setup', NAME, '07', '--tags', tags],
                            text=True, capture_output=True, timeout=5400)
    raw = result.stdout + result.stderr
    path = STATE / ('expected-refusal-' + label + '.log')
    path.write_text(raw); path.chmod(0o600)
    if result.returncode == 0 or marker not in raw or reason not in raw:
        raise ValueError('expected product refusal did not prove its declared cause: ' + label)
    print('ELASTICSEARCH_EXPECTED_REFUSAL_VERIFIED scenario=' + label, flush=True)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_es():
    spec = importlib.util.spec_from_file_location('es_product', BASE / 'roles/cluster-addon/files/elasticsearch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def api():
    return load_es().API(str(DIRECTORY / 'kubectl.kubeconfig'), field_manager='kubeauto-elasticsearch-gate')


def product_health(c):
    # health persists the product identity ledger; it is not a read-only probe.
    es = load_es()
    es.health(c, es.API(str(DIRECTORY / 'kubectl.kubeconfig')))


def release_identity_ownership(topology):
    """Relinquish only the fixture's proven shared product-ledger fields."""
    c, k, es = selected_config(topology), api(), load_es()
    ns, name = c['elasticsearch_namespace'], c['elasticsearch_name'] + '-identity'
    namespaces = json.loads((STATE/'namespaces.json').read_text())
    namespace = k.get('namespace', ns)
    if not namespace or namespace['metadata']['uid'] != namespaces.get(ns):
        raise ValueError('identity ownership recovery refuses an unbound namespace')
    def ledger():
        # kubectl hides managedFields in ordinary JSON output.
        return json.loads(k.run('-n', ns, 'get', 'configmap', name, '-o', 'json', '--show-managed-fields'))

    obj = ledger()
    if not obj or obj['metadata'].get('labels') != es.meta(c, name)['labels']:
        raise ValueError('identity ownership recovery refuses a foreign ledger')

    def fields(value, manager):
        entries = [entry for entry in value['metadata'].get('managedFields', []) if entry['manager'] == manager]
        if any(entry.get('operation') != 'Apply' or entry.get('fieldsType') != 'FieldsV1' or entry.get('subresource') for entry in entries):
            raise ValueError('identity ownership recovery refuses unknown field management')
        def leaves(tree, prefix=()):
            result = set()
            for key, child in tree.items():
                if child: result.update(leaves(child, prefix + (key,)))
                else: result.add(prefix + (key,))
            return result
        return set().union(*(leaves(entry['fieldsV1']) for entry in entries))

    manager = 'kubeauto-elasticsearch-gate'
    held = fields(obj, manager)
    if not held:
        return
    allowed = {('f:data', 'f:' + key) for key in ('cluster_uuid', 'pvc_uids', 'topology', 'version_ceiling')}
    allowed |= {('f:metadata', 'f:labels', 'f:' + key) for key in es.meta(c, name)['labels']}
    if not held <= allowed or not held <= fields(obj, 'kubeauto-elasticsearch'):
        raise ValueError('identity ownership recovery refuses unknown or exclusively held fields')
    claims = {p['metadata']['name']: p['metadata']['uid'] for p in k.get('pvc', namespace=ns)['items']}
    retained = json.loads(obj['data']['pvc_uids'])
    if not retained or any(claims.get(key) != uid for key, uid in retained.items()) or obj['data']['topology'] != topology:
        raise ValueError('identity ownership recovery refuses changed retained PVCs or topology')
    with es.connection(c, k) as request:
        if request('GET', '/')['cluster_uuid'] != obj['data']['cluster_uuid']:
            raise ValueError('identity ownership recovery refuses changed cluster UUID')

    def preserved(value):
        result = dict(value)
        result['metadata'] = {key: item for key, item in value['metadata'].items() if key not in ('managedFields', 'resourceVersion')}
        return result

    metadata = {key: obj['metadata'][key] for key in ('name', 'namespace', 'uid', 'resourceVersion')}
    body = json.dumps({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata})
    command = ('apply', '--server-side', '--field-manager=' + manager, '-f', '-', '-o', 'json', '--show-managed-fields')
    preview = json.loads(k.run(*command, '--dry-run=server', body=body))
    if preserved(preview) != preserved(obj) or fields(preview, manager):
        raise ValueError('identity ownership recovery dry-run changed retained ledger contents')
    current = ledger()
    if current != obj:
        raise ValueError('identity ownership recovery refuses ledger drift after dry-run')
    result = json.loads(k.run(*command, body=body))
    if preserved(result) != preserved(obj) or fields(result, manager) or not held <= fields(result, 'kubeauto-elasticsearch'):
        raise ValueError('identity ownership recovery did not preserve product ownership and contents')
    print('ELASTICSEARCH_IDENTITY_OWNERSHIP_RESTORED topology=' + topology + ' data=unchanged uid=' + metadata['uid'], flush=True)


def configuration(**updates):
    path = DIRECTORY / 'config.yml'
    # Both soak clients select their topology concurrently. Serialize the
    # read/merge/replace, including across fixture processes, and use a private
    # temporary file so one writer cannot remove another writer's input.
    with open(DIRECTORY/'.configuration.lock', 'a', opener=lambda p, flags: os.open(p, flags, 0o600)) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = yaml.safe_load(path.read_text())
        data.update(updates)
        descriptor, name = tempfile.mkstemp(prefix='config.', suffix='.tmp', dir=DIRECTORY)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return data


def baseline():
    record = json.loads((STATE / 'baseline.json').read_text())
    current = api().get('namespace', 'kube-system')['metadata']['uid']
    if record['cluster_uid'] != current or record['hosts'] != list(HOSTS):
        raise ValueError('retained fixture cluster identity differs')
    nodes = api().get('nodes')['items']
    if {n['metadata']['name'] for n in nodes} != set(NODES) or any(n['status']['nodeInfo']['kubeletVersion'] != 'v1.33.6' for n in nodes):
        raise ValueError('retained fixture node identity/version differs')
    return record


def context_identity(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('previous context directory identity is unproven')
    context=directory/'kubectl.kubeconfig'
    if context.is_symlink() or not context.is_file():
        raise ValueError('previous context file identity is unproven')
    stat=directory.stat()
    return {'device':stat.st_dev,'inode':stat.st_ino,'context_sha256':hashlib.sha256(context.read_bytes()).hexdigest()}


def retire_previous_context(directory, expected, owner):
    if expected is None:
        if directory.exists():raise ValueError('unexpected previous context appeared after admission')
        return
    if context_identity(directory)!=expected:
        raise ValueError('previous context changed after admission; preserve for diagnosis')
    archive=STATE.parent/'kubeauto-elasticsearch-history'/owner
    archive.mkdir(parents=True,mode=0o700)
    directory.rename(archive/'retired-k8s-dev')


def archive_state(owner):
    """Only call after native teardown and its clean verification succeeded."""
    archive=STATE.parent/'kubeauto-elasticsearch-history'/owner
    archive.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=archive/'state'
    target.mkdir(mode=0o700)
    # The logging handoff deliberately outlives the source. Everything else
    # belongs to this serialized fixture run, including private raw evidence.
    for path in STATE.iterdir():
        if path.name!='logging-ownership.json':path.rename(target/path.name)
    print('ELASTICSEARCH_STATE_ARCHIVED owner='+owner+' directory='+str(target),flush=True)


def admission():
    """Only the measured, empty disposable cluster may be retired at launch."""
    if DIRECTORY.exists() or (STATE / 'baseline.json').exists():
        raise ValueError('bootstrap refuses retained ES state; use its focused owning module or clean path')
    previous = BASE / 'clusters/k8s-dev/kubectl.kubeconfig'
    if previous.parent.exists() and not previous.is_file():
        raise ValueError('previous source directory has no provable cluster context')
    if previous.exists():
        old = load_es().API(str(previous))
        uid = old.get('namespace', 'kube-system')['metadata']['uid']
        if uid != 'cd102ec9-acd8-43f3-adb6-5f10bca24059':
            raise ValueError('disposable source baseline changed since read-only inventory')
        nodes = old.get('nodes')['items']
        addresses = {a['address'] for n in nodes for a in n['status']['addresses'] if a['type'] == 'InternalIP'}
        if addresses != set(PREVIOUS_HOSTS) or any(n['status']['nodeInfo']['kubeletVersion'] != 'v1.37.1' for n in nodes):
            raise ValueError('disposable source host/version boundary differs')
        namespaces = {o['metadata']['name'] for o in old.get('namespaces')['items']}
        if namespaces != {'default', 'kube-system', 'kube-public', 'kube-node-lease'}:
            raise ValueError('disposable source has foreign namespaces')
        if old.get('pv')['items'] or old.get('pvc')['items']:
            raise ValueError('disposable source has persistent data')
        if any(o['metadata']['namespace'] != 'kube-system' for o in old.get('pods')['items']):
            raise ValueError('disposable source has non-platform workloads')
        execute(['bash', BASE / 'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', *SPARE_HOSTS], capture=False, timeout=600)
    else:
        execute(['bash', BASE / 'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', *HOSTS], capture=False, timeout=1800)
    inventory = {address: host_capacity(address) for address in HOSTS}
    if len({row['product_uuid'] for row in inventory.values()}) != len(HOSTS):
        raise ValueError('source hosts do not have distinct VM hardware identities')
    print('ELASTICSEARCH_SOURCE_ADMISSION_PASS destructive_scope=source-seven-hosts raw_disks=excluded control=preserved', flush=True)
    return inventory


def bootstrap():
    previous = BASE/'clusters/k8s-dev'
    previous_identity = context_identity(previous) if previous.exists() else None
    inventory = admission()
    owner = {'hosts': list(HOSTS), 'hardware': inventory, 'id': str(uuid.uuid4()), 'phase': 'admitted-empty-source', 'previous_context':previous_identity}
    atomic(STATE / 'ownership.json', owner)
    # Product and artifact prerequisites are checked before this host mutation.
    execute(['bash', BASE / 'tests/helpers/lab-wipe-nodes.sh', '--rocky-only', *HOSTS], capture=False, timeout=1800)
    execute(['bash', BASE / 'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', *HOSTS], capture=False, timeout=1800)
    retire_previous_context(previous, previous_identity, owner['id'])
    product('new', NAME)
    inventory = []
    for group, indexes in (('etcd', range(3)), ('kube_master', range(3)), ('kube_node', range(3, len(HOSTS)))):
        inventory += ['[' + group + ']'] + [HOSTS[i] + " k8s_nodename='" + NODES[i] + "'" for i in indexes]
    inventory += ['[all:vars]', 'SECURE_PORT="6443"', 'CONTAINER_RUNTIME="containerd"', 'CLUSTER_NETWORK="calico"', 'PROXY_MODE="ipvs"',
                  'SERVICE_CIDR="10.83.0.0/16"', 'CLUSTER_CIDR="172.30.0.0/16"', 'NODE_PORT_RANGE="30000-32767"',
                  'CLUSTER_DNS_DOMAIN="cluster.local"', 'bin_dir="/usr/local/bin"', 'base_dir="/usr/local/kubeauto"',
                  'cluster_dir="{{ base_dir }}/clusters/elasticsearch-gate"', 'ca_dir="/etc/kubernetes/ssl"', "ansible_user=root", "k8s_nodename=''", '']
    (DIRECTORY / 'hosts').write_text('\n'.join(inventory))
    configuration(K8S_VER='1.33.6', KUBE_RESERVED_ENABLED='no', SYS_RESERVED_ENABLED='no', SYS_RESERVED_ENFORCE='no',
                  REGISTRY_HOST_IP='192.168.122.2', local_path_provisioner_install='no')
    bootstrap_platform()


def bootstrap_resume():
    """Resume only the admitted, still-empty source after a download failure."""
    owner = json.loads((STATE/'ownership.json').read_text())
    if (owner.get('phase') != 'admitted-empty-source' or owner.get('hosts') != list(HOSTS)
            or (STATE/'baseline.json').exists() or (DIRECTORY/'kubectl.kubeconfig').exists()
            or list(STATE.glob('filesystem-*.json'))):
        raise ValueError('bootstrap resume requires the original empty admitted source')
    inventory = (DIRECTORY/'hosts').read_text()
    if re.findall(r'^(192[.]168[.]122[.]\d+) ', inventory, re.M) != list(HOSTS[:3])*2+list(HOSTS[3:]):
        raise ValueError('bootstrap resume inventory changed')
    config = yaml.safe_load((DIRECTORY/'config.yml').read_text())
    expected = dict(K8S_VER='1.33.6', REGISTRY_HOST_IP='192.168.122.2',
                    KUBE_RESERVED_ENABLED='no', SYS_RESERVED_ENABLED='no', SYS_RESERVED_ENFORCE='no',
                    local_path_provisioner_install='no')
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError('bootstrap resume configuration changed')
    for address in HOSTS:
        if host_capacity(address)['product_uuid'] != owner['hardware'][address]['product_uuid']:
            raise ValueError('bootstrap resume hardware identity changed')
    execute(['bash', BASE/'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', *HOSTS], capture=False, timeout=1800)
    bootstrap_platform()


def bootstrap_platform():
    product('download', '-D', timeout=5400)
    product('setup', NAME, '90', timeout=7200)
    k = api()
    nodes = k.get('nodes')['items']
    if (len(nodes) != len(HOSTS) or any(n['status']['nodeInfo']['kubeletVersion'] != 'v1.33.6' or
            not any(condition['type']=='Ready' and condition['status']=='True' for condition in n['status'].get('conditions',[])) for n in nodes)):
        raise ValueError('product cluster did not converge to the pinned platform')
    atomic(STATE / 'baseline.json', {'cluster_uid': k.get('namespace', 'kube-system')['metadata']['uid'], 'hosts': list(HOSTS),
                                  'registry': '192.168.122.2', 'created': time.time(), 'data_path': '/var/lib/kubeauto-elasticsearch-longhorn'})
    # Lab-only fixture: three dedicated ES masters use the disposable K8s
    # control nodes. Production scheduling defaults are not changed.
    k.run('uncordon', *NODES[:3])
    for node in k.get('nodes')['items']:
        if node['metadata']['name'] in NODES[:3] and any(t.get('key') == 'node.kubernetes.io/unschedulable' for t in node.get('spec', {}).get('taints', [])):
            k.run('taint', 'node', node['metadata']['name'], 'node.kubernetes.io/unschedulable:NoSchedule-')
    print('ELASTICSEARCH_PLATFORM_CORE_PASS nodes=' + str(len(HOSTS)) + ' version=1.33.6', flush=True)


def registry_recover():
    """Repair the proven missing Docker bridge; preserve Registry and AIO data."""
    def registry():
        row = json.loads(execute(['docker', 'inspect', 'local_registry']))[0]
        mounts = [m for m in row['Mounts'] if m['Destination'] == '/var/lib/registry']
        if (row['Config']['Image'] != 'brinnatt/registry:2' or len(mounts) != 1
                or mounts[0]['Type'] != 'bind' or mounts[0]['Source'] != '/data/registry'):
            raise ValueError('Registry recovery refuses an unknown data boundary')
        path = Path(mounts[0]['Source']); info = path.stat()
        catalog = execute(['docker', 'exec', 'local_registry', 'wget', '-q', '-T', '5', '-O', '-',
                           'http://127.0.0.1:5000/v2/_catalog?n=1000'])
        repositories = json.loads(catalog)['repositories']
        if len(repositories) >= 1000:
            raise ValueError('Registry recovery refuses an incomplete catalog page')
        return {'id': row['Id'], 'image': row['Image'], 'data': [info.st_dev, info.st_ino],
                'catalog_sha256': hashlib.sha256(json.dumps(sorted(repositories)).encode()).hexdigest()}
    running = execute(['docker', 'ps', '--format', '{{.Names}}']).splitlines()
    if running != ['local_registry'] or Path('/sys/class/net/docker0').exists():
        raise ValueError('Registry recovery requires only local_registry and the proven missing bridge')
    before = registry()
    context = BASE/'clusters/aio/kubectl.kubeconfig'
    if not context.is_file(): raise ValueError('Registry recovery requires the protected AIO context')
    protected = load_es().API(str(context))
    def aio_identity():
        uid = protected.get('namespace', 'kube-system')['metadata']['uid']
        nodes = protected.get('nodes')['items']
        if not nodes or any(not any(c['type']=='Ready' and c['status']=='True'
                for c in n['status'].get('conditions', [])) for n in nodes):
            raise ValueError('protected AIO is not healthy')
        return {'uid': uid, 'nodes': sorted(n['metadata']['uid'] for n in nodes)}
    aio = aio_identity()
    atomic(STATE/'registry-recovery.json', {'before':before, 'aio':aio, 'phase':'intent'})
    execute(['systemctl', 'restart', 'docker'], capture=False, timeout=180)
    deadline = time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            with urlopen('http://127.0.0.1:5000/v2/', timeout=2) as response:
                if response.status == 200: break
        except OSError: pass
        time.sleep(2)
    else: raise ValueError('Registry HTTP did not recover')
    if not Path('/sys/class/net/docker0').exists() or registry()!=before or aio_identity()!=aio:
        raise ValueError('Registry recovery changed protected identities or data')
    atomic(STATE/'registry-recovery.json', {'before':before, 'aio':aio, 'phase':'complete'})
    print('ELASTICSEARCH_REGISTRY_RECOVERY_SCOPED_CLEAN_PASS registry_data=preserved aio=preserved', flush=True)


def host(address, argv, *, body=None, timeout=180):
    if address not in HOSTS:
        raise ValueError('fixture refuses non-source host')
    return execute(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'root@' + address, shlex.join(argv)], body=body, timeout=timeout)


def host_capacity(address):
    code = ('import json,os,platform,time,shutil; from pathlib import Path; '
        'print(json.dumps({"cpu":os.cpu_count(),"memory":os.sysconf("SC_PHYS_PAGES")*os.sysconf("SC_PAGE_SIZE"),'
        '"free":shutil.disk_usage("/").free,"kernel":platform.release(),"arch":platform.machine(),'
        '"product_uuid":Path("/sys/class/dmi/id/product_uuid").read_text().strip(),"time":time.time()}))')
    row = json.loads(host(address, ['python3', '-c', code]))
    kernel = tuple(int(part) for part in row['kernel'].split('-')[0].split('.')[:2])
    if (row['cpu'] < 8 or row['memory'] < 14*1024**3 or row['free'] < 16*1024**3 or kernel < (5, 4) or
            row['arch'] != 'x86_64' or not row['product_uuid'] or abs(row['time']-time.time()) > 10):
        raise ValueError('source host capacity, kernel, architecture or clock admission failed: ' + address)
    return row


def filesystem(address, action):
    spec = importlib.util.spec_from_file_location('es_filesystem', BASE / 'tests/helpers/elasticsearch-filesystem.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    record_path = STATE / ('filesystem-' + address + '.json')
    if record_path.exists():
        request = json.loads(record_path.read_text())
    else:
        if action != 'prepare':
            return
        request = {'id': str(uuid.uuid4()), 'uuid': str(uuid.uuid4()), 'size': 32 * 1024**3,
                   'machine_id': host(address, ['cat', '/etc/machine-id']).strip()}
        atomic(record_path, request)
    code = inspect.getsource(module.host_filesystem) + '\nimport sys,json\nprint(json.dumps(host_filesystem(json.loads(sys.stdin.read()), ' + repr(action) + ')))'
    result = json.loads(host(address, ['python3', '-c', code], body=json.dumps(request)))
    if action == 'remove':
        if result.get('removed') is not True:
            raise ValueError('filesystem cleanup did not confirm removal')
        record_path.unlink()
    return result



def sysctl_fixture(action):
    spec = importlib.util.spec_from_file_location('es_filesystem', BASE / 'tests/helpers/elasticsearch-filesystem.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for address in HOSTS:
        path = STATE / ('sysctl-' + address + '.json')
        if action == 'snapshot' and path.exists():
            continue
        if action == 'restore' and not path.exists():
            continue
        request = json.loads(path.read_text()) if path.exists() else None
        code = inspect.getsource(module.host_sysctl) + '\nimport json,sys\nprint(json.dumps(host_sysctl(json.loads(sys.stdin.read()), ' + repr(action) + ')))'
        result = json.loads(host(address, ['python3', '-c', code], body=json.dumps(request)))
        if action == 'snapshot': atomic(path, result)
        elif result.get('restored') is True: path.unlink()
        else: raise ValueError('sysctl cleanup did not confirm restoration')


def bind_namespace(name):
    obj = api().get('namespace', name)
    if not obj or not obj['metadata'].get('uid'): raise ValueError('fixture namespace is not established')
    path = STATE/'namespaces.json'
    record = json.loads(path.read_text()) if path.exists() else {}
    if name in record and record[name] != obj['metadata']['uid']: raise ValueError('fixture namespace was replaced')
    record[name] = obj['metadata']['uid']
    atomic(path, record)


def cleanup_namespaces():
    path = STATE/'namespaces.json'
    record = json.loads(path.read_text()) if path.exists() else {}
    platform = {'default', 'kube-system', 'kube-public', 'kube-node-lease'}
    for obj in api().get('namespaces')['items']:
        name = obj['metadata']['name']
        if name not in platform and record.get(name) != obj['metadata']['uid']:
            raise ValueError('source cleanup refuses foreign or replaced namespace: ' + name)


def cleanup_workload_namespaces():
    """Keep ECK and both storage controllers alive until their consumers exit."""
    k=api();record=json.loads((STATE/'namespaces.json').read_text())
    protected={'elastic-system','longhorn-system','local-path-storage','es-standard','es-compact'}
    # Check the complete set again before the first delete, including names
    # already absent after a focused scenario's own cleanup.
    candidates=[]
    for name,uid in record.items():
        if name in protected:continue
        obj=k.get('namespace',name)
        if not obj:continue
        if obj['metadata']['uid']!=uid:raise ValueError('workload cleanup refuses replaced namespace')
        candidates.append((name,obj))
    for name,obj in candidates:
        options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{
            'uid':obj['metadata']['uid'],'resourceVersion':obj['metadata']['resourceVersion']}}
        k.run('delete','--raw','/api/v1/namespaces/'+name,'-f','-',body=json.dumps(options))
        k.run('wait','--for=delete','namespace/'+name,'--timeout=10m',timeout=660)
    execute([BASE/'.venv/bin/python',BASE/'roles/cluster-addon/files/eck.py','can-remove',
             '--kubeconfig',DIRECTORY/'kubectl.kubeconfig'],capture=False)

def storage_inventory():
    """Allow the leased masters to be CSI clients, keeping their node names."""
    path = DIRECTORY/'hosts'
    if path.is_symlink(): raise ValueError('storage inventory refuses a symlink')
    original = path.read_text()
    sections = {}
    for group in ('etcd', 'kube_master', 'kube_node'):
        matches = list(re.finditer(r'^\[' + group + r'\]\n(.*?)(?=^\[|\Z)', original, re.M | re.S))
        if len(matches) != 1: raise ValueError('storage inventory requires one owned group: ' + group)
        sections[group] = matches[0]
    for group in ('etcd', 'kube_master'):
        expected = [HOSTS[i] + " k8s_nodename='" + NODES[i] + "'" for i in range(3)]
        if sections[group][1].splitlines() != expected:
            raise ValueError('storage inventory master identity changed')
    workers = [HOSTS[i] + " k8s_nodename='" + NODES[i] + "'" for i in range(3, len(HOSTS))]
    clients = list(HOSTS[:3]) + workers
    section = sections['kube_node']
    current = section[1].splitlines()
    if current not in (workers, clients): raise ValueError('storage inventory client identity changed')
    if current != clients:
        updated = original[:section.start(1)] + '\n'.join(clients) + '\n' + original[section.end(1):]
        temporary = path.with_name('hosts.storage.tmp')
        with temporary.open('x') as stream: stream.write(updated)
        temporary.replace(path)
    print('ELASTICSEARCH_STORAGE_INVENTORY_PASS clients=' + str(len(HOSTS)) + ' node_names=preserved', flush=True)


def storage_credentials():
    """Prepare the administrator-owned mTLS input before native preflight."""
    k = api(); cluster = baseline()['cluster_uid']; ns = 'longhorn-system'
    path = STATE/'storage-credentials.json'
    record = json.loads(path.read_text()) if path.exists() else None
    namespaces = STATE/'namespaces.json'
    owned = json.loads(namespaces.read_text()) if namespaces.exists() else {}
    namespace = k.get('namespace', ns)
    if namespace:
        if owned.get(ns) != namespace['metadata']['uid']:
            raise ValueError('storage credentials refuse an unowned namespace')
    elif record or ns in owned:
        raise ValueError('storage credential namespace disappeared')
    secret = k.get('secret', 'longhorn-grpc-tls', ns) if namespace else None
    if secret:
        identity = {'cluster_uid': cluster, 'namespace_uid': namespace['metadata']['uid'],
                    'secret_uid': secret['metadata']['uid'],
                    'data_sha256': hashlib.sha256(json.dumps(secret['data'], sort_keys=True).encode()).hexdigest()}
        if identity != record:
            raise ValueError('storage credentials refuse changed or unowned Secret')
    elif record:
        raise ValueError('storage credential Secret disappeared')
    else:
        spec = importlib.util.spec_from_file_location('es_storage_tls', BASE/'tests/helpers/longhorn-lab.py')
        fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
        data = {key: base64.b64encode(value.encode()).decode() for key, value in fixture.tls_fixture().items()}
        secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
            'name': 'longhorn-grpc-tls', 'namespace': ns, 'labels': LABELS},
            'type': 'kubernetes.io/tls', 'data': data}
    spec = importlib.util.spec_from_file_location('es_storage_product', BASE/'roles/longhorn/files/longhorn.py')
    native = importlib.util.module_from_spec(spec); spec.loader.exec_module(native)
    with tempfile.TemporaryDirectory(prefix='es-storage-tls-') as temporary:
        for key in ('ca.crt', 'tls.crt', 'tls.key'):
            target = Path(temporary)/key
            target.write_bytes(base64.b64decode(secret['data'][key], validate=True)); target.chmod(0o600)
        native.verify_grpc_tls(temporary)
    if not namespace:
        k.run('create', '-f', '-', '-o', 'json', body=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace',
            'metadata': {'name': ns, 'labels': dict(LABELS, **{'kubeauto.io/component': 'longhorn'})}}))
        bind_namespace(ns)
        namespace = k.get('namespace', ns)
    if not record:
        created = json.loads(k.run('create', '-f', '-', '-o', 'json', body=json.dumps(secret)))
        atomic(path, {'cluster_uid': cluster, 'namespace_uid': namespace['metadata']['uid'],
                      'secret_uid': created['metadata']['uid'],
                      'data_sha256': hashlib.sha256(json.dumps(created['data'], sort_keys=True).encode()).hexdigest()})
    print('ELASTICSEARCH_STORAGE_CREDENTIALS_PASS mtls=verified identity=bound', flush=True)


def storage():
    baseline()
    storage_inventory()
    storage_credentials()
    for address in HOSTS:
        filesystem(address, 'prepare')
    configuration(longhorn_install='yes', longhorn_action='present', longhorn_version='1.12.1',
                  longhorn_storage_nodes=list(HOSTS), longhorn_data_engines=['v1'],
                  longhorn_data_path='/data/kubeauto-longhorn-elasticsearch-v1', longhorn_min_disk_gib=20,
                  longhorn_default_replica_count=3,
                  longhorn_storage_classes=[dict(name='es-test', replicas=3, fs_type='ext4', reclaim_policy='Delete', default=False, encrypted=False, encryption_secret='', recurring_jobs=[])])
    product('download', '-E', 'longhorn', timeout=5400)
    product('setup', NAME, '09', timeout=5400)
    if not api().get('storageclass', 'es-test'):
        raise ValueError('product Longhorn StorageClass is absent')
    bind_namespace('longhorn-system')
    print('ELASTICSEARCH_PLATFORM_STORAGE_PASS driver=driver.longhorn.io engine=v1 version=1.12.1', flush=True)


def cleanup_csi():
    path=STATE/'csi.json'
    if not path.exists():return
    record=json.loads(path.read_text());k=api();ns=record['namespace']
    obj=k.get('namespace',ns)
    if obj:
        if obj['metadata']['uid']!=record['namespace_uid'] or not all(obj['metadata'].get('labels',{}).get(a)==b for a,b in LABELS.items()):
            raise ValueError('CSI cleanup refuses changed namespace identity')
        k.run('delete','namespace',ns,'--wait=true','--timeout=10m',timeout=660)
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:
        retained=[pv for pv in k.get('pv')['items'] if pv.get('spec',{}).get('claimRef',{}).get('namespace')==ns]
        if not retained:break
        print('ELASTICSEARCH_WAIT_HEARTBEAT cleanup=CSI-probe-PV',flush=True);time.sleep(5)
    else:raise ValueError('CSI probe PV remains after scoped cleanup')
    record['clean']=True;atomic(path,record)
    print('ELASTICSEARCH_CSI_SCOPED_CLEAN_PASS',flush=True)


def csi_probe():
    """Qualify the product CSI class before any Elasticsearch data is placed."""
    baseline();k=api();path=STATE/'csi.json'
    if path.exists() and not json.loads(path.read_text()).get('clean'):
        raise ValueError('CSI probe requires recovery of its retained owned namespace')
    token=secrets.token_hex(8);ns='es-csi-'+token
    if k.get('namespace',ns):raise ValueError('CSI probe namespace already exists')
    k.apply([{'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns,'labels':LABELS}}]);bind_namespace(ns)
    record={'namespace':ns,'namespace_uid':k.get('namespace',ns)['metadata']['uid'],'cluster_uid':baseline()['cluster_uid']}
    atomic(path,record)
    k.apply([{'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':'probe','namespace':ns,'labels':LABELS},
              'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'es-test','resources':{'requests':{'storage':'1Gi'}}}}])
    rows=json.loads((BASE/'tests/evidence/elasticsearch-images-20261103.json').read_text())['images']
    from common.constants import KubeConstant
    image=next(row for row in rows if row['key']=='brinnatt/elasticsearch-client:'+KubeConstant().v_elasticsearch_client)
    image=image['talkedu_ref'].rsplit(':',1)[0]+'@'+image['digest']
    payload=hashlib.sha256(token.encode()).hexdigest()*16384
    expected=hashlib.sha256(payload.encode()).hexdigest()
    for ordinal,node in enumerate(NODES[3:5]):
        script=('import hashlib,json,os,time,socket\nfrom pathlib import Path\np=Path("/probe/data")\n'
            +'socket.gethostbyname("kubernetes.default.svc")\nsocket.create_connection(("kubernetes.default.svc",443),timeout=10).close()\n'
            +(f'with p.open("xb") as f:\n f.write({payload[:64]!r}.encode()*16384);f.flush();os.fsync(f.fileno())\n'
              'd=os.open("/probe",os.O_RDONLY);os.fsync(d);os.close(d)\n' if ordinal==0 else '')
            +f'assert hashlib.sha256(p.read_bytes()).hexdigest()=={expected!r}\n'
            +'print("CSI_FSYNC_DATA_VERIFIED",flush=True)\nPath("/tmp/ready").touch()\nwhile True:time.sleep(10)\n')
        name='probe-'+str(ordinal)
        pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':ns,'labels':LABELS},'spec':{
            'nodeSelector':{'kubernetes.io/hostname':node},'restartPolicy':'Never','automountServiceAccountToken':False,
            'securityContext':{'runAsNonRoot':True,'runAsUser':1000,'runAsGroup':1000,'fsGroup':1000},
            'containers':[{'name':'probe','image':image,'command':['python3','-c',script],
                'resources':{'requests':{'cpu':'50m','memory':'64Mi'},'limits':{'memory':'128Mi'}},
                'readinessProbe':{'exec':{'command':['python3','-c','from pathlib import Path;assert Path("/tmp/ready").exists()']}},
                'volumeMounts':[{'name':'probe','mountPath':'/probe'}]}],
            'volumes':[{'name':'probe','persistentVolumeClaim':{'claimName':'probe'}}]}}
        k.apply([pod]);k.run('-n',ns,'wait','--for=condition=Ready','pod/'+name,'--timeout=10m',timeout=660)
        current=k.get('pod',name,ns);binding=volume_binding(current,attached_node=node)
        pvc=k.get('pvc','probe',ns)
        if pvc['spec']['accessModes']!=['ReadWriteOnce']:raise ValueError('CSI probe is not RWO')
        if ordinal and binding!=record['binding']:raise ValueError('CSI reattach replaced the original data volume')
        if 'CSI_FSYNC_DATA_VERIFIED' not in k.run('-n',ns,'logs',name).splitlines():raise ValueError('CSI probe lacks fsync/hash evidence')
        if ordinal==0:
            record.update(binding=binding,first_node=node);atomic(path,record)
            options={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':current['metadata']['uid'],'resourceVersion':current['metadata']['resourceVersion']}}
            k.run('delete','--raw','/api/v1/namespaces/'+ns+'/pods/'+name,'-f','-',body=json.dumps(options))
            k.run('-n',ns,'wait','--for=delete','pod/'+name,'--timeout=5m',timeout=330)
            continue
        before=int(k.run('-n',ns,'exec',name,'--','python3','-c','import os;s=os.statvfs("/probe");print(s.f_blocks*s.f_frsize)').strip())
        k.run('-n',ns,'patch','pvc','probe','--type=merge','-p',json.dumps({'spec':{'resources':{'requests':{'storage':'2Gi'}}}}))
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            pvc=k.get('pvc','probe',ns);pv=k.get('pv',binding['pv']);native=k.get('volumes.longhorn.io',binding['volume'],'longhorn-system')
            after=int(k.run('-n',ns,'exec',name,'--','python3','-c','import os;s=os.statvfs("/probe");print(s.f_blocks*s.f_frsize)').strip())
            if (load_es().quantity(pvc.get('status',{}).get('capacity',{}).get('storage','0'))>=2*1024**3 and
                    load_es().quantity(pv['spec']['capacity']['storage'])>=2*1024**3 and int(native['spec']['size'])>=2*1024**3 and
                    after>before and after>=1.8*1024**3):break
            print('ELASTICSEARCH_WAIT_HEARTBEAT CSI=online-filesystem-expansion',flush=True);time.sleep(5)
        else:raise ValueError('CSI online expansion did not reach claim/PV/native/filesystem')
        if volume_binding(k.get('pod',name,ns),attached_node=node)!=binding:raise ValueError('CSI expansion replaced original binding')
        actual=k.run('-n',ns,'exec',name,'--','python3','-c','import hashlib;print(hashlib.sha256(open("/probe/data","rb").read()).hexdigest())').strip()
        if actual!=expected:raise ValueError('CSI expansion changed fsynced contents')
        record.update(second_node=node,sha256=expected,filesystem_before=before,filesystem_after=after,phase='complete');atomic(path,record)
    cleanup_csi()
    print('ELASTICSEARCH_CSI_MEASUREMENT '+json.dumps(record,sort_keys=True),flush=True)
    print('ELASTICSEARCH_CASE_PASS id=ES-09',flush=True)
    print('ELASTICSEARCH_CASE_PASS id=ES-07',flush=True)



def selected_config(topology, **updates):
    if topology not in ('standard', 'compact'):
        raise ValueError('unknown topology')
    c = json.loads((STATE / ('config-' + topology + '.json')).read_text())
    c.update(updates)
    configuration(**c)
    if updates:
        atomic(STATE / ('config-' + topology + '.json'), c)
    return c


def data_ledger(topology, *, create=False):
    c = selected_config(topology)
    es, k = load_es(), api()
    path = STATE / ('ledger-' + topology + '.json')
    if path.exists():
        record = json.loads(path.read_text())
    elif create:
        token = json.loads((STATE/'ownership.json').read_text())['id'].replace('-', '')
        record = {'index': 'es-gate-confirmed-' + token, 'documents': {str(i): {'business_id': str(i), 'value': i*17} for i in range(32)}}
        atomic(path, record)
    else:
        raise ValueError('confirmation ledger is missing')
    index, expected = record['index'], record['documents']
    with es.connection(c, k) as request:
        exists = request('GET', '/' + index + '/_settings?ignore_unavailable=true')
        if not exists:
            if not create: raise ValueError('confirmed index disappeared')
            request('PUT', '/' + index, {'settings': {'number_of_shards': 3, 'number_of_replicas': 1},
                    'mappings': {'dynamic': 'strict', 'properties': {'business_id': {'type': 'keyword'}, 'value': {'type': 'integer'}}}})
            state = request('GET', '/_cluster/health/' + index + '?wait_for_status=green&timeout=60s')
            if state.get('timed_out') or state['status'] != 'green': raise ValueError('confirmation index replicas unavailable')
            for identifier, document in expected.items():
                result = request('PUT', '/' + index + '/_create/' + identifier + '?wait_for_active_shards=all', document)
                if result.get('_shards', {}).get('failed') or result.get('_shards', {}).get('successful', 0) < 2:
                    raise ValueError('confirmation ledger write was not acknowledged by the expected copies')
            request('POST', '/' + index + '/_refresh')
        rows = request('POST', '/' + index + '/_mget', {'ids': sorted(expected)})['docs']
        actual = {r['_id']: r['_source'] for r in rows if r.get('found')}
        if actual != expected or request('GET', '/' + index + '/_count')['count'] != len(expected):
            raise ValueError('confirmed document ledger differs')
        uuid_now = request('GET', '/' + index + '/_settings')[index]['settings']['index']['uuid']
        if record.get('index_uuid') and record['index_uuid'] != uuid_now:
            raise ValueError('confirmed index identity changed')
        record['index_uuid'] = uuid_now
        record['sha256'] = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        atomic(path, record)
    print('ELASTICSEARCH_LEDGER_VERIFIED topology=' + topology + ' documents=' + str(len(expected)) + ' sha256=' + record['sha256'], flush=True)
    return record


def topology_state(topology, *, config=None):
    c, k = selected_config(topology) if config is None else config, api()
    ns, name = c['elasticsearch_namespace'], c['elasticsearch_name']
    obj = k.get('elasticsearch', name, ns)
    claims = k.get('pvc', namespace=ns)['items']
    pods = [p for p in k.get('pods', namespace=ns)['items'] if p['metadata'].get('labels', {}).get('elasticsearch.k8s.elastic.co/cluster-name') == name]
    operator = [p for p in k.get('pods', namespace='elastic-system')['items'] if p['metadata']['name'].startswith('elastic-operator-')]
    secret = k.get('secret', name + '-es-elastic-user', ns)
    return {'cr_uid': obj['metadata']['uid'], 'pvc_uids': {p['metadata']['name']: p['metadata']['uid'] for p in claims},
            'pod_uids': {p['metadata']['name']: p['metadata']['uid'] for p in pods + operator},
            'credentials_uid': secret['metadata']['uid'],
            'credentials_sha256': hashlib.sha256(json.dumps(secret['data'], sort_keys=True).encode()).hexdigest()}


def verify_topology(topology):
    c, k, es = selected_config(topology), api(), load_es()
    product_health(c)
    ns, name = c['elasticsearch_namespace'], c['elasticsearch_name']
    pods = [p for p in k.get('pods', namespace=ns)['items'] if p['metadata'].get('labels', {}).get('elasticsearch.k8s.elastic.co/cluster-name') == name]
    groups = {p['metadata']['name']: p['metadata']['labels'] for p in pods}
    targets = {e['targetRef']['name'] for obj in k.get('endpointslices', namespace=ns)['items']
               if obj['metadata'].get('labels', {}).get('kubernetes.io/service-name') == name + '-client'
               for e in obj.get('endpoints', []) if e.get('conditions', {}).get('ready') is True}
    data = {p for p, labels in groups.items() if labels.get('elasticsearch.k8s.elastic.co/node-data') == 'true'}
    if not data or targets != data or len(data) != es.data_count(c):
        raise ValueError('business Service endpoints do not match only ready data members')
    with es.connection(c, k) as request:
        members = request('GET', '/_nodes')['nodes']
        if len(members) != es.member_count(c):
            raise ValueError('actual member count differs from the product topology')
        masters = [m for m in members.values() if 'master' in m['roles']]
        if len(masters) != 3 or topology == 'standard' and any(any(r.startswith('data') for r in m['roles']) for m in masters):
            raise ValueError('actual master and data role separation differs')
    if c['elasticsearch_kibana_enabled']:
        kibana = k.get('kibana', name, ns)
        if kibana['spec']['count'] != 2 or kibana.get('status', {}).get('availableNodes') != 2:
            raise ValueError('optional Kibana did not converge to two available instances')
    return c


def qualify_install(topology):
    c = verify_topology(topology)
    data_ledger(topology, create=True)
    before = topology_state(topology)
    product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
    verify_topology(topology)
    data_ledger(topology)
    if topology_state(topology) != before:
        raise ValueError('same product configuration unexpectedly replaced Pods, credentials, CR or PVC')
    ids = (10, 11, 12) if topology == 'standard' else (13, 14)
    for cid in ids: print('ELASTICSEARCH_CASE_PASS id=ES-' + str(cid), flush=True)
    print('ELASTICSEARCH_INSTALL_SCOPED_CLEAN_PASS topology=' + topology + ' baseline=healthy-owned', flush=True)


def verify_mmap(c):
    measurements={}
    nodes=set(c['elasticsearch_data_nodes']+c['elasticsearch_master_nodes'])
    for name in sorted(nodes):
        address=HOSTS[NODES.index(name)]
        code=('from pathlib import Path;import json\n'
              'print(json.dumps({"runtime":int(Path("/proc/sys/vm/max_map_count").read_text()),'
              '"persistent":Path("/etc/sysctl.d/99-kubeauto-elasticsearch.conf").read_text()}))')
        measured=json.loads(host(address,['python3','-c',code]))
        persistent=[line.split('=',1)[1] for line in measured['persistent'].splitlines() if line.startswith('vm.max_map_count=')]
        if measured['runtime']<1048576 or len(persistent)!=1 or int(persistent[0])<1048576:
            raise ValueError('product mmap runtime/persistent setting is below its accepted threshold')
        measurements[name]={'runtime':measured['runtime'],'persistent':int(persistent[0])}
    atomic(STATE/'mmap.json',{'cluster_uid':baseline()['cluster_uid'],'nodes':measurements})
    print('ELASTICSEARCH_MMAP_MEASUREMENT '+json.dumps(measurements,sort_keys=True),flush=True)
    print('ELASTICSEARCH_CASE_PASS id=ES-08',flush=True)


def scenario_namespace(ns):
    """Keep fixture labels separate from the product's SSA ownership."""
    k = api(); labels = dict(LABELS, **load_es().LABELS)
    obj = k.get('namespace', ns)
    if obj:
        path = STATE/'namespaces.json'
        record = json.loads(path.read_text()) if path.exists() else {}
        current = obj['metadata'].get('labels', {})
        if (record.get(ns) != obj['metadata']['uid'] or
                any(current.get(key) != value for key, value in load_es().LABELS.items()) or
                current.get('kubeauto.io/test-owner') not in (None, LABELS['kubeauto.io/test-owner'])):
            raise ValueError('foreign scenario namespace')
        if 'kubeauto.io/test-owner' not in current:
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': obj['metadata']['uid']},
                     {'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
                     {'op': 'add', 'path': '/metadata/labels/kubeauto.io~1test-owner', 'value': LABELS['kubeauto.io/test-owner']}]
            k.run('patch', 'namespace', ns, '--type=json', '--field-manager=kubeauto-elasticsearch-gate', '-p', json.dumps(patch))
            print('ELASTICSEARCH_NAMESPACE_OWNERSHIP_RESTORED namespace=' + ns, flush=True)
    else:
        k.apply([{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns, 'labels': labels}}])
    bind_namespace(ns)


def install(topology):
    baseline()
    k, es = api(), load_es()
    ns = 'es-' + topology
    scenario_namespace(ns)
    config = configuration(elasticsearch_install='yes', elasticsearch_state='present', elasticsearch_namespace=ns,
                elasticsearch_name='search', elasticsearch_topology=topology,
                elasticsearch_data_nodes=list(NODES[3:]), elasticsearch_data_count=3,
                elasticsearch_master_nodes=list(NODES[:3]) if topology == 'standard' else [],
                elasticsearch_master_cpu='1', elasticsearch_master_memory='2Gi', elasticsearch_master_pvc_size='2Gi',
                elasticsearch_data_cpu='1', elasticsearch_data_memory='4Gi', elasticsearch_data_heap='2g', elasticsearch_data_pvc_size='4Gi',
                elasticsearch_storage_class='es-test', elasticsearch_tolerate_control_plane=True,
                elasticsearch_exporter_enabled=False, elasticsearch_kibana_enabled=topology == 'standard', elasticsearch_requests=[])
    sysctl_fixture('snapshot')
    product('download', '-E', 'elasticsearch', timeout=5400)
    product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
    product_health(config)
    bind_namespace('elastic-system')
    atomic(STATE / ('config-' + topology + '.json'), {key: value for key, value in config.items() if key.startswith('elasticsearch_')})
    verify_mmap(config)
    qualify_install(topology)
    print('ELASTICSEARCH_INSTALL_PASS topology=' + topology, flush=True)


def client_objects(c, module, token):
    if module not in ('business', 'lifecycle', 'security', 'password-rotation', 'unavailable-write', 'network', 'soak', 'rally', 'cleanup-probe') or len(token) != 16 or any(ch not in '0123456789abcdef' for ch in token):
        raise ValueError('unsupported client module or fixture token')
    ns = c['elasticsearch_namespace']
    name = 'es-client-' + token
    labels = dict(LABELS, **load_es().LABELS)
    script = BASE / ('tests/helpers/elasticsearch-rally.py' if module == 'rally' else 'tests/helpers/elasticsearch-business.py')
    configmap = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name, 'namespace': ns, 'labels': labels}, 'data': {'run.py': script.read_text()}}
    pod = {'automountServiceAccountToken': False, 'restartPolicy': 'Never',
           'containers': [{'name': 'client', 'image': 'registry.talkschool.cn:5000/brinnatt/elasticsearch-client:9.5.1-kubeauto.1',
              'command': ['python', '/scripts/run.py', module, '--token', token],
              'env': [{'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'},
                      {'name': 'ELASTICSEARCH_URL', 'value': 'https://' + c['elasticsearch_name'] + '-client.' + ns + '.svc:9200'},
                      {'name': 'ELASTICSEARCH_CA', 'value': '/ca/ca.crt'},
                      {'name': 'ELASTICSEARCH_PASSWORD_FILE', 'value': '/identity/password' if c['elasticsearch_admin_secret'] else '/identity/elastic'}],
              'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'memory': '512Mi'}},
              'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
              'volumeMounts': [{'name': n, 'mountPath': p, 'readOnly': True} for n, p in (('script', '/scripts'), ('identity', '/identity'), ('ca', '/ca'))]}],
           'volumes': [{'name': 'script', 'configMap': {'name': name}},
                       {'name': 'identity', 'secret': {'secretName': c['elasticsearch_admin_secret'] or c['elasticsearch_name'] + '-es-elastic-user'}},
                       {'name': 'ca', 'secret': {'secretName': c['elasticsearch_http_tls_secret'] or c['elasticsearch_name'] + '-es-http-certs-public', 'items': [{'key': 'ca.crt' if c['elasticsearch_http_tls_secret'] else 'tls.crt', 'path': 'ca.crt'}]}}]}
    if c['elasticsearch_admin_secret']:
        pod['containers'][0]['env'].append({'name': 'ELASTICSEARCH_USERNAME_FILE', 'value': '/identity/username'})
    if module == 'unavailable-write':
        address = str(ipaddress.IPv4Address(c['probe_ip']))
        if c['probe_fault'] not in ('quorum', 'shard-loss', 'flood-watermark'):
            raise ValueError('unsupported data safety fault')
        env = pod['containers'][0]['env']
        next(e for e in env if e['name'] == 'ELASTICSEARCH_URL')['value'] = 'https://' + address + ':9200'
        env.extend([{'name': 'ELASTICSEARCH_ASSERT_HOSTNAME', 'value': c['elasticsearch_name']+'-client.'+c['elasticsearch_namespace']+'.svc'},
                    {'name': 'ELASTICSEARCH_FAULT_INDEX', 'value': c['probe_index']}, {'name': 'ELASTICSEARCH_FAULT_KIND', 'value': c['probe_fault']}])
    if module in ('security', 'password-rotation'):
        pod['volumes'].append({'name': 'limited', 'secret': {'secretName': 'es-gate-limited'}})
        pod['containers'][0]['volumeMounts'].append({'name': 'limited', 'mountPath': '/limited', 'readOnly': True})
    if module == 'password-rotation':
        pod['volumes'].append({'name': 'previous', 'secret': {'secretName': 'es-gate-previous-password'}})
        pod['containers'][0]['volumeMounts'].append({'name': 'previous', 'mountPath': '/previous', 'readOnly': True})
    if module == 'network':
        probe_ns, expected = c['probe_namespace'], c['probe_expected']
        if not probe_ns.startswith('es-client-') or expected not in ('allowed', 'denied'):
            raise ValueError('network probe has an invalid fixture boundary')
        ns = probe_ns
        configmap['metadata']['namespace'] = ns
        configmap['data']['run.py'] = (BASE/'tests/helpers/elasticsearch-network.py').read_text()
        container = pod['containers'][0]
        container['command'] = ['python', '/scripts/run.py']
        container['env'] = [e for e in container['env'] if e['name'] in ('PYTHONDONTWRITEBYTECODE', 'ELASTICSEARCH_URL', 'ELASTICSEARCH_CA')]
        container['env'].append({'name': 'ELASTICSEARCH_NETWORK_EXPECTED', 'value': expected})
        container['volumeMounts'] = [v for v in container['volumeMounts'] if v['name'] != 'identity']
        pod['volumes'] = [v for v in pod['volumes'] if v['name'] != 'identity']
        next(v for v in pod['volumes'] if v['name'] == 'ca').update(configMap={'name': 'es-gate-network-ca'})
        next(v for v in pod['volumes'] if v['name'] == 'ca').pop('secret')
    if module == 'rally':
        pod['securityContext'] = {'fsGroup': 1000}
        container = pod['containers'][0]
        container.update(image='registry.talkschool.cn:5000/brinnatt/elasticsearch-rally:2.13.0-kubeauto.1',
                         command=['python3', '/scripts/run.py'], resources={'requests': {'cpu': '500m', 'memory': '512Mi'}, 'limits': {'memory': '2Gi'}})
        container['securityContext']['runAsUser'] = 1000
        for volume, mount in (('work', '/work'), ('rally-state', '/rally/.rally'), ('temporary', '/tmp')):
            pod['volumes'].append({'name': volume, 'emptyDir': {}})
            container['volumeMounts'].append({'name': volume, 'mountPath': mount})
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'namespace': ns, 'labels': labels},
           'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 2250 if module == 'soak' else 1800 if module == 'rally' else 600, 'template': {'metadata': {'labels': labels}, 'spec': pod}}}
    return configmap, job


def client_job(topology, module, *, heartbeat=None, probe_namespace=None, probe_expected=None, fault_pod=None, fault_kind=None, fault_index=None, cleanup_outcome=None):
    baseline()
    k = api()
    c = json.loads((STATE / ('config-' + topology + '.json')).read_text())
    if module == 'network': c.update(probe_namespace=probe_namespace, probe_expected=probe_expected)
    if module == 'unavailable-write':
        obj = k.get('pod', fault_pod['metadata']['name'], c['elasticsearch_namespace'])
        if (not obj or obj['metadata']['uid'] != fault_pod['metadata']['uid'] or
                obj['metadata'].get('labels', {}).get('elasticsearch.k8s.elastic.co/cluster-name') != c['elasticsearch_name']):
            raise ValueError('fault diagnostic Pod identity changed')
        c.update(probe_ip=obj['status']['podIP'], probe_fault=fault_kind, probe_index=fault_index)
    configmap, job = client_objects(c, module, secrets.token_hex(8))
    if module=='cleanup-probe':
        if cleanup_outcome not in ('normal','failed','interrupted'):raise ValueError('explicit cleanup fixture outcome required')
        command='print("ELASTICSEARCH_CLEANUP_PROBE_STARTED",flush=True)\n'
        command+=('print("ELASTICSEARCH_BUSINESS_SCOPED_CLEAN_PASS module=cleanup-probe",flush=True)' if cleanup_outcome=='normal' else
                  'raise SystemExit(73)' if cleanup_outcome=='failed' else 'import time;time.sleep(600)')
        job['spec']['template']['spec']['containers'][0]['command']=['python3','-c',command]
    ns = job['metadata']['namespace']
    name, labels = job['metadata']['name'], job['metadata']['labels']
    if k.get('job', name, ns) or k.get('configmap', name, ns):
        raise ValueError('client fixture identity already exists')
    try:
        k.apply([configmap, job])
        started = time.monotonic()
        deadline = started + (2280 if module == 'soak' else 1830 if module == 'rally' else 630)
        while time.monotonic() < deadline:
            obj = k.get('job', name, ns)
            if not obj or not all(obj['metadata'].get('labels', {}).get(a) == b for a, b in labels.items()):
                raise ValueError('client Job ownership changed')
            if obj.get('status', {}).get('failed'):
                if cleanup_outcome=='failed':
                    pods=k.get('pods',namespace=ns)['items']
                    codes=[status.get('state',{}).get('terminated',{}).get('exitCode') for pod in pods
                           if pod['metadata'].get('labels',{}).get('job-name')==name for status in pod.get('status',{}).get('containerStatuses',[])]
                    if codes!=[73]:raise ValueError('cleanup failure fixture did not reach its exact intentional exit')
                    raise CleanupProbeFailure('intentional cleanup fixture exit=73')
                raise ValueError('client Job failed; retain its first raw log before cleanup')
            if cleanup_outcome=='interrupted' and obj.get('status',{}).get('active'):
                pods=k.get('pods',namespace=ns)['items']
                running=any('running' in status.get('state',{}) for pod in pods
                            if pod['metadata'].get('labels',{}).get('job-name')==name
                            for status in pod.get('status',{}).get('containerStatuses',[])
                            if status.get('name')=='client')
                if running:
                    raw=k.run('-n',ns,'logs','job/'+name,'--all-containers=true')
                    if 'ELASTICSEARCH_CLEANUP_PROBE_STARTED' in raw.splitlines():
                        raise InterruptedError('intentional running cleanup fixture interruption')
            if any(v.get('type') == 'Complete' and v.get('status') == 'True' for v in obj.get('status', {}).get('conditions', [])):
                break
            if heartbeat is not None: heartbeat(time.monotonic()-started)
            print('ELASTICSEARCH_WAIT_HEARTBEAT client=' + module + ' topology=' + topology, flush=True)
            time.sleep(5)
        else:
            raise ValueError('client Job completion timed out')
        raw = k.run('-n', ns, 'logs', 'job/' + name, '--all-containers=true')
        require_markers = ({'ELASTICSEARCH_RALLY_RACE_PASS'} if module == 'rally' else {'ELASTICSEARCH_BUSINESS_SCOPED_CLEAN_PASS module=' + module})
        if module == 'network': require_markers = {'ELASTICSEARCH_NETWORK_PROBE_PASS expected=' + probe_expected}
        if module == 'business':
            require_markers.update('ELASTICSEARCH_CASE_PASS id=ES-' + str(i) for i in range(15, 21))
        elif module == 'lifecycle':
            require_markers.update('ELASTICSEARCH_CASE_PASS id=ES-' + str(i) for i in (21, 22))
        elif module == 'security':
            require_markers.update('ELASTICSEARCH_CASE_PASS id=ES-' + str(i) for i in (24, 25))
        elif module == 'soak':
            require_markers.add('ELASTICSEARCH_CASE_PASS id=ES-49')
        if not require_markers <= set(raw.splitlines()) or any(marker in raw for marker in ('ELASTICSEARCH_BUSINESS_FAIL', 'ELASTICSEARCH_RALLY_FAIL', 'ELASTICSEARCH_NETWORK_PROBE_FAIL')):
            raise ValueError('client Job lacks complete business or cleanup evidence')
        print(raw, end='', flush=True)
    finally:
        first_error = sys.exc_info()[1]
        if first_error is not None:
            try:
                diagnostics(STATE / (name + '-first-failure'))
            except Exception as diagnostic_error:
                print('ELASTICSEARCH_CLIENT_DIAGNOSTICS_FAIL reason=' + type(diagnostic_error).__name__, file=sys.stderr, flush=True)
        try:
            raw = k.run('-n', ns, 'logs', 'job/' + name, '--all-containers=true')
            path = STATE / (name + '.log')
            path.write_text(raw)
            path.chmod(0o600)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass
        try:
            # Validate both names before removing either object.
            found = []
            for kind in ('job', 'configmap'):
                obj = k.get(kind, name, ns)
                if obj:
                    if not all(obj['metadata'].get('labels', {}).get(a) == b for a, b in labels.items()):
                        raise ValueError('foreign client object during cleanup')
                    found.append(kind)
            for kind in found:
                k.run('-n', ns, 'delete', kind, name, '--wait=true', '--timeout=120s', timeout=150)
            if k.get('job', name, ns) or k.get('configmap', name, ns):
                raise ValueError('client fixture objects remain')
        except Exception as cleanup_error:
            if first_error is None:
                raise
            print('ELASTICSEARCH_CLIENT_CLEANUP_FAIL reason=' + type(cleanup_error).__name__, file=sys.stderr, flush=True)
    print('ELASTICSEARCH_CLIENT_SCOPED_CLEAN_PASS topology=' + topology + ' module=' + module, flush=True)
    return raw


class CleanupProbeFailure(ValueError):
    pass


def cleanup_probes():
    baseline();k=api();c=verify_topology('compact');ns=c['elasticsearch_namespace']
    before=topology_state('compact')
    def clients():
        return {kind:{obj['metadata']['name']:obj['metadata']['uid'] for obj in k.get(kind,namespace=ns)['items']}
                for kind in ('jobs','configmaps')}
    existing=clients()
    for outcome in ('normal','failed','interrupted'):
        try:
            client_job('compact','cleanup-probe',cleanup_outcome=outcome)
        except CleanupProbeFailure:
            if outcome!='failed':raise
        except InterruptedError as error:
            if outcome!='interrupted' or str(error)!='intentional running cleanup fixture interruption':raise
        else:
            if outcome!='normal':raise ValueError('cleanup fixture did not reach its intended refusal/interruption')
        if clients()!=existing:raise ValueError('cleanup probe removed foreign clients or retained owned objects')
        if topology_state('compact')!=before:raise ValueError('cleanup probe changed retained product identities')
        verify_topology('compact');data_ledger('compact')
        print('ELASTICSEARCH_CLEANUP_OUTCOME_PASS outcome='+outcome+' owned_clients=removed product=preserved',flush=True)
    print('ELASTICSEARCH_CASE_PASS id=ES-56',flush=True)
    print('ELASTICSEARCH_CLEANUP_PROBES_SCOPED_CLEAN_PASS outcomes=3',flush=True)


def business():
    for topology in ('standard', 'compact'):
        client_job(topology, 'business')



def lifecycle():
    baseline()
    for topology in ('standard', 'compact'):
        client_job(topology, 'lifecycle')
    topology = 'compact'
    c = selected_config(topology, elasticsearch_smartcn_enabled=True)
    product('download', '-E', 'elasticsearch-smartcn', timeout=5400)
    try:
        for attempt in range(2):
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
            verify_topology(topology)
            data_ledger(topology)
            with load_es().connection(c, api()) as request:
                plugins = request('GET', '/_nodes/plugins')['nodes']
                if not plugins or any(not any(p['name'] == 'analysis-smartcn' and p['elasticsearch_version'] == c['elasticsearch_ver'] for p in n['plugins']) for n in plugins.values()):
                    raise ValueError('same-version SmartCN is missing from a member')
                tokens = request('POST', '/_analyze', {'analyzer': 'smartcn', 'text': '中华人民共和国'})['tokens']
                if not tokens or ''.join(t['token'] for t in tokens) != '中华人民共和国':
                    raise ValueError('SmartCN token content differs')
            current = topology_state(topology)
            if attempt == 0: before = current
            elif current != before: raise ValueError('SmartCN reentry unexpectedly replaced retained identities')
        print('ELASTICSEARCH_CASE_PASS id=ES-23', flush=True)
    finally:
        primary = sys.exc_info()[1]
        try:
            selected_config(topology, elasticsearch_smartcn_enabled=False)
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
            verify_topology(topology); data_ledger(topology)
        except Exception as error:
            if primary is None: raise
            print('ELASTICSEARCH_SMARTCN_RESTORE_FAIL reason=' + type(error).__name__, file=sys.stderr)
    print('ELASTICSEARCH_LIFECYCLE_SCOPED_CLEAN_PASS baseline=healthy-owned', flush=True)


def certificate_data(c, *, invalid_san=False):
    """Short-lived lab CA and service leaf; private bytes never reach output."""
    with tempfile.TemporaryDirectory(prefix='es-gate-http-') as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        execute(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '3',
            '-keyout', directory/'ca.key', '-out', directory/'ca.crt', '-subj', '/CN=es-gate-' + secrets.token_hex(8),
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign'])
        execute(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', directory/'tls.key',
            '-out', directory/'leaf.csr', '-subj', '/CN=elasticsearch-gate'])
        hosts = ['invalid.example'] if invalid_san else [service + '.' + c['elasticsearch_namespace'] + '.svc' + suffix
            for service in (c['elasticsearch_name']+'-client', c['elasticsearch_name']+'-es-http') for suffix in ('', '.cluster.local')]
        extension = directory/'extensions'
        extension.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=' + ','.join('DNS:'+h for h in hosts)+'\n')
        execute(['openssl', 'x509', '-req', '-in', directory/'leaf.csr', '-CA', directory/'ca.crt', '-CAkey', directory/'ca.key',
            '-set_serial', str(secrets.randbits(120)), '-out', directory/'tls.crt', '-days', '3', '-sha256', '-extfile', extension])
        return {name: base64.b64encode((directory/name).read_bytes()).decode() for name in ('ca.crt', 'tls.crt', 'tls.key')}


def certificate_rotation(topology):
    c, k, es = selected_config(topology), api(), load_es()
    ns = c['elasticsearch_namespace']
    names = ('es-gate-http-a', 'es-gate-http-b', 'es-gate-http-invalid')
    if any(k.get('secret', name, ns) for name in names):
        raise ValueError('certificate fixture name is already occupied')
    created = {}
    try:
        first, second = certificate_data(c), certificate_data(c)
        prior = k.get('secret', c['elasticsearch_http_tls_secret'] or c['elasticsearch_name']+'-es-http-certs-public', ns)['data']
        prior_ca = base64.b64decode(prior.get('ca.crt') or prior['tls.crt'])
        for name, data, old_ca in ((names[0], first, prior_ca), (names[1], second, base64.b64decode(first['ca.crt']))):
            bundle = dict(data, **{'ca.crt': base64.b64encode(old_ca + b'\n' + base64.b64decode(data['ca.crt'])).decode()})
            k.apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': ns, 'labels': LABELS}, 'type': 'kubernetes.io/tls', 'data': bundle}])
            created[name] = k.get('secret', name, ns)['metadata']['uid']
            selected_config(topology, elasticsearch_http_tls_secret=name)
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
            verify_topology(topology); data_ledger(topology)
            client_job(topology, 'business')
        # Drop the old CA only after new connections succeeded with overlap.
        k.apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': names[1], 'namespace': ns, 'labels': LABELS}, 'type': 'kubernetes.io/tls', 'data': second}])
        product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
        verify_topology(topology); data_ledger(topology)
        try:
            with es.connection(c | {'elasticsearch_http_tls_secret': names[0]}, k) as request: request('GET', '/')
        except ssl.SSLCertVerificationError:
            pass
        else:
            raise ValueError('old CA still authenticated the replacement server certificate')
        invalid = certificate_data(c, invalid_san=True)
        k.apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': names[2], 'namespace': ns, 'labels': LABELS}, 'type': 'kubernetes.io/tls', 'data': invalid}])
        created[names[2]] = k.get('secret', names[2], ns)['metadata']['uid']
        before = topology_state(topology)
        selected_config(topology, elasticsearch_http_tls_secret=names[2])
        product_refusal('HTTP certificate chain, validity or service SAN validation failed', 'http-san-' + topology)
        if topology_state(topology) != before: raise ValueError('invalid certificate changed the protected topology')
    finally:
        primary = sys.exc_info()[1]
        try:
            selected_config(topology, elasticsearch_http_tls_secret=c['elasticsearch_http_tls_secret'])
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
            verify_topology(topology); data_ledger(topology)
            for name, uid in created.items():
                obj = k.get('secret', name, ns)
                if obj:
                    if obj['metadata']['uid'] != uid: raise ValueError('certificate fixture identity was replaced')
                    k.run('-n', ns, 'delete', 'secret', name, '--wait=true')
                if k.get('secret', name, ns): raise ValueError('certificate fixture remains')
        except Exception as error:
            if primary is None: raise
            print('ELASTICSEARCH_CERTIFICATE_RESTORE_FAIL reason=' + type(error).__name__, file=sys.stderr)
    print('ELASTICSEARCH_CASE_PASS id=ES-26', flush=True)


def security_certificate_restore():
    """Restore certificate fixtures without re-running identity or installation."""
    baseline()
    if list(STATE.glob('fault-*.json')):
        raise ValueError('certificate recovery requires fault journals restored first')
    k, es = api(), load_es()
    identities = json.loads((STATE/'namespaces.json').read_text())
    names = ('es-gate-http-a', 'es-gate-http-b', 'es-gate-http-invalid')
    plans = []
    # Admit both topologies before the first configuration change or deletion.
    for topology in ('standard', 'compact'):
        c = json.loads((STATE/('config-'+topology+'.json')).read_text())
        ns = c['elasticsearch_namespace']
        namespace = k.get('namespace', ns)
        if not namespace or namespace['metadata']['uid'] != identities.get(ns):
            raise ValueError('certificate recovery namespace identity changed')
        fixtures = {name: k.get('secret', name, ns) for name in names}
        fixtures = {name: obj for name, obj in fixtures.items() if obj}
        if any(not obj['metadata'].get('uid') or obj.get('type') != 'kubernetes.io/tls' or
               not all(obj['metadata'].get('labels', {}).get(key) == value for key, value in LABELS.items())
               for obj in fixtures.values()):
            raise ValueError('certificate recovery refuses foreign fixture')
        original = c['elasticsearch_http_tls_secret']
        cluster = k.get('elasticsearch', c['elasticsearch_name'], ns)
        if not cluster or cluster.get('status', {}).get('health') != 'green':
            raise ValueError('certificate recovery requires a healthy retained cluster')
        current = cluster['spec'].get('http', {}).get('tls', {}).get('certificate', {}).get('secretName', '')
        if original in names or current not in (original, *fixtures) or c.get('elasticsearch_requests'):
            raise ValueError('certificate recovery refuses unknown original, reference or pending requests')
        ledger = json.loads((STATE/('ledger-'+topology+'.json')).read_text())
        index, expected = ledger['index'], ledger['documents']
        # The live server may still use fixture B while the saved desired
        # configuration correctly names the original certificate.
        with es.connection(c | {'elasticsearch_http_tls_secret': current}, k) as request:
            rows = request('POST', '/' + index + '/_mget', {'ids': sorted(expected)})['docs']
            actual = {row['_id']: row['_source'] for row in rows if row.get('found')}
            count = request('GET', '/' + index + '/_count')['count']
            index_uid = request('GET', '/' + index + '/_settings')[index]['settings']['index']['uuid']
            if actual != expected or count != len(expected) or index_uid != ledger['index_uuid']:
                raise ValueError('certificate recovery confirmation ledger differs')
        before = topology_state(topology, config=c)
        plans.append((topology, c, current, fixtures, before))
    for topology, c, current, fixtures, before in plans:
        ns = c['elasticsearch_namespace']
        if current != c['elasticsearch_http_tls_secret']:
            selected_config(topology)
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
        verify_topology(topology); data_ledger(topology)
        after = topology_state(topology)
        # ECK may roll Pods during a certificate change. Persistent product
        # identities and the confirmed data must survive that legitimate roll.
        if any(after[key] != before[key] for key in ('cr_uid', 'pvc_uids', 'credentials_uid', 'credentials_sha256')):
            raise ValueError('certificate recovery changed retained product identities')
        live = k.get('elasticsearch', c['elasticsearch_name'], ns)
        restored = live['spec'].get('http', {}).get('tls', {}).get('certificate', {}).get('secretName', '')
        if restored != c['elasticsearch_http_tls_secret']:
            raise ValueError('certificate recovery did not restore the original reference')
        if k.get('namespace', ns)['metadata']['uid'] != identities[ns]:
            raise ValueError('certificate recovery namespace identity changed before cleanup')
        for name, old in fixtures.items():
            obj = k.get('secret', name, ns)
            if not obj or obj['metadata']['uid'] != old['metadata']['uid']:
                raise ValueError('certificate recovery fixture identity changed')
            k.run('-n', ns, 'delete', 'secret', name, '--wait=true', '--timeout=120s', timeout=150)
            if k.get('secret', name, ns):
                raise ValueError('certificate recovery fixture remains')
    print('ELASTICSEARCH_CERTIFICATE_RESTORE_SCOPED_CLEAN_PASS', flush=True)


def network_policy_probes():
    for topology in ('standard', 'compact'):
        c, k = selected_config(topology), api()
        if not load_es().flag(c['elasticsearch_network_policy_enabled']):
            raise ValueError('network qualification requires the product policy enabled')
        namespace = 'es-client-' + topology
        if k.get('namespace', namespace): raise ValueError('network fixture namespace is already occupied')
        uid = None
        try:
            k.apply([{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace, 'labels': LABELS}}])
            uid = k.get('namespace', namespace)['metadata']['uid']
            bind_namespace(namespace)
            certificate = k.get('secret', c['elasticsearch_http_tls_secret'] or c['elasticsearch_name']+'-es-http-certs-public', c['elasticsearch_namespace'])['data']
            ca = base64.b64decode(certificate.get('ca.crt') or certificate['tls.crt']).decode()
            k.apply([{'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'es-gate-network-ca', 'namespace': namespace, 'labels': LABELS}, 'data': {'ca.crt': ca}}])
            client_job(topology, 'network', probe_namespace=namespace, probe_expected='denied')
            k.run('label', 'namespace', namespace, 'kubeauto.io/elasticsearch-client=true')
            client_job(topology, 'network', probe_namespace=namespace, probe_expected='allowed')
            k.run('label', 'namespace', namespace, 'kubeauto.io/elasticsearch-client-')
            client_job(topology, 'network', probe_namespace=namespace, probe_expected='denied')
        finally:
            primary = sys.exc_info()[1]
            try:
                obj = k.get('namespace', namespace)
                if obj:
                    if not uid or obj['metadata']['uid'] != uid: raise ValueError('network namespace identity changed')
                    k.run('delete', 'namespace', namespace, '--wait=true', '--timeout=120s', timeout=150)
                if k.get('namespace', namespace): raise ValueError('network namespace remains')
                data_ledger(topology)
            except Exception as error:
                if primary is None: raise
                print('ELASTICSEARCH_NETWORK_CLEANUP_FAIL reason=' + type(error).__name__, file=sys.stderr)
    print('ELASTICSEARCH_CASE_PASS id=ES-29', flush=True)
    print('ELASTICSEARCH_NETWORK_SCOPED_CLEAN_PASS', flush=True)


def security_identity():
    baseline()
    for topology in ('standard', 'compact'):
        c, k = selected_config(topology), api()
        ns = c['elasticsearch_namespace']
        name = 'es-gate-limited'
        secret = k.get('secret', name, ns)
        if secret and not all(secret['metadata'].get('labels', {}).get(key) == value for key, value in LABELS.items()):
            raise ValueError('foreign least-privilege fixture identity')
        if not secret:
            k.apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': ns, 'labels': LABELS},
                      'type': 'Opaque', 'stringData': {'username': 'es-gate-limited', 'password': secrets.token_urlsafe(32)}}])
        requests = [{'id': 'gate-limited-role', 'type': 'role', 'name': name,
                     'definition': {'cluster': ['manage_own_api_key'], 'indices': [{'names': ['es-gate-allowed-*'], 'privileges': ['read']}]}},
                    {'id': 'gate-limited-user', 'type': 'user', 'name': name, 'password_secret': name, 'roles': [name]}]
        previous_name = 'es-gate-previous-password'
        if k.get('secret', previous_name, ns):
            raise ValueError('previous-password fixture name is already occupied')
        try:
            selected_config(topology, elasticsearch_requests=requests)
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
            client_job(topology, 'security')
            previous = k.get('secret', name, ns)
            k.apply([{'apiVersion': 'v1', 'kind': 'Secret',
                'metadata': {'name': previous_name, 'namespace': ns, 'labels': LABELS},
                'type': 'Opaque', 'data': previous['data']}])
            k.apply([{'apiVersion': 'v1', 'kind': 'Secret',
                'metadata': {'name': name, 'namespace': ns, 'labels': LABELS}, 'type': 'Opaque',
                'stringData': {'username': name, 'password': secrets.token_urlsafe(32)}}])
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
            client_job(topology, 'password-rotation')
            print('ELASTICSEARCH_CASE_PASS id=ES-27', flush=True)
        finally:
            primary = sys.exc_info()[1]
            try:
                with load_es().connection(c, k) as request:
                    request('DELETE', '/_security/user/' + name)
                    request('DELETE', '/_security/role/' + name)
                selected_config(topology, elasticsearch_requests=[])
                retained = k.get('secret', name, ns)
                if retained and all(retained['metadata'].get('labels', {}).get(key) == value for key, value in LABELS.items()):
                    k.run('-n', ns, 'delete', 'secret', name, '--wait=true')
                elif retained: raise ValueError('fixture identity ownership changed during cleanup')
                if k.get('secret', name, ns): raise ValueError('fixture credentials remain')
                previous = k.get('secret', previous_name, ns)
                if previous:
                    if not all(previous['metadata'].get('labels', {}).get(key) == value for key, value in LABELS.items()):
                        raise ValueError('previous-password fixture ownership changed')
                    k.run('-n', ns, 'delete', 'secret', previous_name, '--wait=true')
                if k.get('secret', previous_name, ns): raise ValueError('previous-password fixture remains')
                data_ledger(topology)
            except Exception as error:
                if primary is None: raise
                print('ELASTICSEARCH_SECURITY_CLEANUP_FAIL reason=' + type(error).__name__, file=sys.stderr)
    print('ELASTICSEARCH_SECURITY_IDENTITY_SCOPED_CLEAN_PASS', flush=True)


def security_certificates():
    baseline()
    for topology in ('standard', 'compact'):
        certificate_rotation(topology)
    print('ELASTICSEARCH_SECURITY_CERTIFICATES_SCOPED_CLEAN_PASS', flush=True)


def security_network():
    baseline()
    network_policy_probes()


def security_s3():
    baseline()
    s3_rotation()


def security():
    security_identity()
    security_certificates()
    security_network()
    security_s3()
    print('ELASTICSEARCH_SECURITY_SCOPED_CLEAN_PASS', flush=True)


def fault_record(kind, name, namespace, replicas):
    k = api()
    if kind == 'statefulset' and namespace == 'elastic-system':
        obj = json.loads(k.run('-n', namespace, 'get', kind, name, '--show-managed-fields=true', '-o', 'json'))
    else:
        obj = k.get(kind, name, namespace)
    if not obj or not obj['metadata'].get('uid') or obj['spec']['replicas'] != replicas:
        raise ValueError('fault refuses an unexpected controller baseline')
    if kind == 'statefulset' and namespace == 'elastic-system':
        if not any(m.get('manager') == 'kubeauto-eck-operator' for m in obj['metadata'].get('managedFields', [])):
            raise ValueError('fault refuses foreign shared Operator')
    elif not any(r.get('kind') == 'Elasticsearch' for r in obj['metadata'].get('ownerReferences', [])):
        raise ValueError('fault refuses a controller without Elasticsearch ownership')
    path = STATE / ('fault-' + namespace + '-' + name + '.json')
    if path.exists(): raise ValueError('unrecovered fault journal exists')
    atomic(path, {'kind': kind, 'namespace': namespace, 'name': name, 'uid': obj['metadata']['uid'], 'replicas': replicas})
    return path


def restore_faults():
    k = api()
    if (STATE/'dr-disaster.json').exists():
        spec=importlib.util.spec_from_file_location('es_dr_recovery',BASE/'tests/helpers/elasticsearch-dr.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        module.fence_target_writer(sys.modules[__name__])
    if (STATE/'fault-s3-rotation.json').exists():
        restore_s3_rotation()
    if (STATE/'fault-s3-negative.json').exists():
        restore_s3_negative()
    if (STATE/'fault-observability.json').exists():
        restore_observability()
    for path in sorted(STATE.glob('fault-worker-*.json')):
        restore_worker(path)
    # Restore desired member counts before resuming ECK. Restarted members
    # cannot become Ready until ECK clears their pre-stop shutdown markers.
    paths = [p for p in STATE.glob('fault-*.json') if not p.name.startswith(('fault-worker-', 'fault-watermarks-')) and p.name not in ('fault-s3-rotation.json','fault-s3-negative.json','fault-observability.json')]
    records = [(path, json.loads(path.read_text())) for path in paths]
    records.sort(key=lambda item: (item[1]['namespace'] == 'elastic-system', item[0].name))
    for path, record in records:
        obj = k.get(record['kind'], record['name'], record['namespace'])
        if not obj or obj['metadata']['uid'] != record['uid']:
            raise ValueError('fault recovery refuses changed controller identity')
    for path, record in records:
        k.run('-n', record['namespace'], 'scale', record['kind'] + '/' + record['name'], '--replicas=' + str(record['replicas']))
    for path, record in reversed(records):
        k.run('-n', record['namespace'], 'wait', '--for=jsonpath={.status.readyReplicas}=' + str(record['replicas']),
              record['kind'] + '/' + record['name'], '--timeout=10m', timeout=660)
        now = k.get(record['kind'], record['name'], record['namespace'])
        if not now or now['metadata']['uid'] != record['uid'] or now['spec']['replicas'] != record['replicas'] or now.get('status', {}).get('readyReplicas', 0) != record['replicas']:
            raise ValueError('fault recovery controller did not converge')
        path.unlink()
    for path in sorted(STATE.glob('fault-watermarks-*.json')):
        restore_watermarks(path)


def guest_fence(record, action):
    # Reuse the qualified guest fencing primitive without changing Longhorn's
    # delivered code. Its per-token guards/receipt never authorize power fencing.
    spec = importlib.util.spec_from_file_location('es_guest_fence', BASE/'tests/helpers/longhorn-node-failure.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    code = inspect.getsource(module.guest_fence) + '\nimport json,sys\nprint(json.dumps(guest_fence(json.load(sys.stdin), '+repr(action)+')))'
    return json.loads(host(record['address'], ['python3', '-c', code], body=json.dumps(record['fence']), timeout=600))


def fenced_worker(record):
    proof = guest_fence(record, 'probe')
    if proof.get('fenced') is not True or proof.get('kubepods_pids') != [] or proof.get('scope') != 'guest-runtime':
        raise ValueError('worker positive guest fence no longer holds')
    node = api().get('node', record['node'])
    if not node or node['metadata']['uid'] != record['node_uid']:
        raise ValueError('fenced worker node identity changed')
    if not node.get('spec', {}).get('unschedulable') or not any(
            c['type'] == 'Ready' and c['status'] in ('False', 'Unknown') for c in node.get('status', {}).get('conditions', [])):
        raise ValueError('fenced worker must remain cordoned and NotReady')


def volume_binding(pod, *, attached_node=None):
    k, ns = api(), pod['metadata']['namespace']
    claims = [v['persistentVolumeClaim']['claimName'] for v in pod['spec']['volumes'] if 'persistentVolumeClaim' in v]
    if len(claims) != 1: raise ValueError('worker fault requires the single ES data claim')
    pvc = k.get('pvc', claims[0], ns)
    pv = k.get('pv', pvc['spec']['volumeName'])
    reference = pv['spec']['claimRef']
    if (reference.get('uid'), reference.get('namespace'), reference.get('name')) != (pvc['metadata']['uid'], ns, claims[0]):
        raise ValueError('worker fault PVC/PV binding identity differs')
    csi = pv['spec'].get('csi', {})
    if csi.get('driver') != 'driver.longhorn.io': raise ValueError('worker fixture requires the qualified Longhorn CSI')
    volume = k.get('volumes.longhorn.io', csi['volumeHandle'], 'longhorn-system')
    if not volume: raise ValueError('worker fixture native volume is absent')
    if attached_node:
        attachments = [a for a in k.get('volumeattachments.storage.k8s.io')['items']
            if a.get('spec', {}).get('source', {}).get('persistentVolumeName') == pv['metadata']['name'] and a.get('status', {}).get('attached')]
        if (len(attachments) != 1 or attachments[0]['spec'].get('nodeName') != attached_node or
                attachments[0]['spec'].get('attacher') != 'driver.longhorn.io' or
                volume.get('status', {}).get('state') != 'attached' or volume['status'].get('currentNodeID') != attached_node):
            raise ValueError('worker migration lacks one safe CSI/native attachment to the actual receiver')
    return {'pvc': claims[0], 'pvc_uid': pvc['metadata']['uid'], 'pv': pv['metadata']['name'], 'pv_uid': pv['metadata']['uid'],
            'volume': volume['metadata']['name'], 'volume_uid': volume['metadata']['uid']}


def restore_worker(path):
    record, k = json.loads(path.read_text()), api()
    obj = k.get('node', record['node'])
    if not obj or obj['metadata']['uid'] != record['node_uid']: raise ValueError('worker recovery node identity changed')
    if record.get('fence_intent'):
        if guest_fence(record, 'restore').get('restored') is not True: raise ValueError('worker guest fence was not restored')
    deadline = time.monotonic()+600
    while time.monotonic() < deadline:
        obj = k.get('node', record['node'])
        if not obj or obj['metadata']['uid'] != record['node_uid']: raise ValueError('worker recovery node was replaced')
        if any(c['type'] == 'Ready' and c['status'] == 'True' for c in obj.get('status', {}).get('conditions', [])): break
        print('ELASTICSEARCH_WAIT_HEARTBEAT recovery=worker-ready', flush=True); time.sleep(5)
    else: raise ValueError('worker did not return Ready')
    if record.get('cordon_intent'):
        k.run('uncordon', record['node'])
    deadline = time.monotonic()+180
    while time.monotonic() < deadline:
        obj = k.get('node', record['node'])
        if not obj or obj['metadata']['uid'] != record['node_uid']: raise ValueError('worker scheduling identity changed')
        if not obj.get('spec', {}).get('unschedulable') and obj.get('spec', {}).get('taints', []) == record['taints']: break
        print('ELASTICSEARCH_WAIT_HEARTBEAT recovery=worker-scheduling', flush=True); time.sleep(3)
    else: raise ValueError('worker original scheduling was not restored')
    path.unlink()


def worker_fault():
    k, es = api(), load_es()
    for topology in ('standard', 'compact'): verify_topology(topology); data_ledger(topology)
    cleanup_namespaces()
    c = selected_config('standard')
    pods = [p for p in k.get('pods', namespace=c['elasticsearch_namespace'])['items']
        if p['metadata'].get('labels', {}).get('elasticsearch.k8s.elastic.co/statefulset-name') == c['elasticsearch_name']+'-es-data']
    pod = next((p for p in pods if p['spec']['nodeName'] in NODES[3:]), None)
    if not pod: raise ValueError('worker fault lacks an actual data member')
    node_name = pod['spec']['nodeName']; address = HOSTS[NODES.index(node_name)]
    node = k.get('node', node_name)
    if node.get('spec', {}).get('unschedulable') or node.get('spec', {}).get('taints', []):
        raise ValueError('worker fault requires the original untainted schedulable node')
    running = [p for p in k.get('pods')['items'] if p.get('spec', {}).get('nodeName') == node_name]
    allowed = {'kube-system', 'longhorn-system', 'es-standard', 'es-compact'}
    if any(p['metadata']['namespace'] not in allowed for p in running): raise ValueError('worker fence refuses another namespace consumer')
    affected = [p for p in running if p['metadata']['namespace'] in ('es-standard', 'es-compact') and
        p['metadata'].get('labels', {}).get('elasticsearch.k8s.elastic.co/cluster-name') == 'search']
    if not affected: raise ValueError('worker fence lacks its original ES member')
    record = {'node': node_name, 'node_uid': node['metadata']['uid'], 'address': address, 'taints': node.get('spec', {}).get('taints', []),
        'fence': {'token': secrets.token_hex(16), 'pod_uids': sorted(p['metadata']['uid'] for p in running),
            'host': json.loads(host(address, ['python3', '-c', 'import json;from pathlib import Path;print(json.dumps({"machine_id":Path("/etc/machine-id").read_text().strip(),"boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip()}))']))},
        'pods': [{'name': p['metadata']['name'], 'uid': p['metadata']['uid'], 'namespace': p['metadata']['namespace'], 'binding': volume_binding(p, attached_node=node_name)} for p in affected]}
    record['fence']['original'] = guest_fence(record, 'inspect')
    path = STATE/('fault-worker-'+node_name+'.json')
    if path.exists(): raise ValueError('worker fault has an unrecovered journal')
    atomic(path, record)
    try:
        record['cordon_intent'] = True; atomic(path, record)
        k.run('cordon', node_name)
        record['fence_intent'] = True; atomic(path, record)
        proof = guest_fence(record, 'fence')
        if proof.get('fenced') is not True or proof.get('kubepods_pids') != []: raise ValueError('worker lacks positive guest runtime fencing')
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            obj = k.get('node', node_name)
            if not obj or obj['metadata']['uid'] != record['node_uid']: raise ValueError('worker fault node identity changed')
            if any(s['type'] == 'Ready' and s['status'] in ('False', 'Unknown') for s in obj.get('status', {}).get('conditions', [])): break
            print('ELASTICSEARCH_WAIT_HEARTBEAT fault=worker-notready', flush=True); time.sleep(3)
        else: raise ValueError('fenced worker did not become NotReady')
        for old in record['pods']:
            # SSH loss or NotReady alone never authorizes force retirement.
            fenced_worker(record)
            obj = k.get('pod', old['name'], old['namespace'])
            if not obj or obj['metadata']['uid'] != old['uid'] or obj['spec']['nodeName'] != node_name:
                raise ValueError('worker refuses changed original member before retirement')
            options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'gracePeriodSeconds': 0,
                'preconditions': {'uid': old['uid'], 'resourceVersion': obj['metadata']['resourceVersion']}}
            k.run('delete', '--raw', '/api/v1/namespaces/'+old['namespace']+'/pods/'+old['name'], '-f', '-', body=json.dumps(options))
        deadline = time.monotonic()+1200
        while time.monotonic() < deadline:
            migrated = []
            for old in record['pods']:
                now = k.get('pod', old['name'], old['namespace'])
                if not now or now['metadata']['uid'] == old['uid'] or now.get('spec', {}).get('nodeName') in (None, node_name): break
                if not any(s['type'] == 'Ready' and s['status'] == 'True' for s in now.get('status', {}).get('conditions', [])): break
                if volume_binding(now, attached_node=now['spec']['nodeName']) != old['binding']:
                    raise ValueError('worker migration changed the original PVC/PV/native volume')
                migrated.append({'namespace': old['namespace'], 'pod': old['name'], 'from': node_name, 'to': now['spec']['nodeName'], 'binding': old['binding']})
            if len(migrated) == len(record['pods']): break
            print('ELASTICSEARCH_WAIT_HEARTBEAT fault=worker-PVC-migration', flush=True); time.sleep(5)
        else: raise ValueError('worker members did not migrate their original volumes to another node')
        fenced_worker(record)
        for topology in ('standard', 'compact'):
            verify_topology(topology); data_ledger(topology); client_job(topology, 'business')
        print('ELASTICSEARCH_WORKER_MIGRATION '+json.dumps({'fence_scope': 'guest-runtime', 'power_fence': False, 'members': migrated}, sort_keys=True), flush=True)
    finally:
        primary = sys.exc_info()[1]
        try:
            restore_faults()
            for topology in ('standard', 'compact'): verify_topology(topology); data_ledger(topology)
        except Exception as error:
            if primary is None: raise
            print('ELASTICSEARCH_WORKER_RESTORE_FAIL reason='+type(error).__name__, file=sys.stderr)
    print('ELASTICSEARCH_CASE_PASS id=ES-31', flush=True)
    print('ELASTICSEARCH_WORKER_SCOPED_CLEAN_PASS original_node=ready scheduling=restored', flush=True)


def primary_pod_fault(topology):
    c, k, es = selected_config(topology), api(), load_es()
    record = data_ledger(topology)
    with es.connection(c, k) as request:
        rows = request('GET', '/_cat/shards/' + record['index'] + '?format=json')
        target = next((r['node'] for r in rows if r['prirep'] == 'p' and r['state'] == 'STARTED'), None)
    if not target: raise ValueError('fault cannot identify an active primary holder')
    pod = k.get('pod', target, c['elasticsearch_namespace'])
    if not pod or pod['metadata'].get('labels', {}).get('elasticsearch.k8s.elastic.co/cluster-name') != c['elasticsearch_name']:
        raise ValueError('primary fault Pod ownership differs')
    old_uid = pod['metadata']['uid']
    k.run('-n', c['elasticsearch_namespace'], 'delete', 'pod', target, '--wait=true', '--timeout=120s', timeout=150)
    product_health(c)
    replaced = k.get('pod', target, c['elasticsearch_namespace'])
    if not replaced or replaced['metadata']['uid'] == old_uid:
        raise ValueError('primary fault did not replace its exact Pod')
    data_ledger(topology)
    print('ELASTICSEARCH_CASE_PASS id=ES-30', flush=True)


def operator_fault():
    k = api()
    for topology in ('standard', 'compact'): data_ledger(topology)
    fault_record('statefulset', 'elastic-operator', 'elastic-system', 1)
    try:
        k.run('-n', 'elastic-system', 'scale', 'statefulset/elastic-operator', '--replicas=0')
        k.run('-n', 'elastic-system', 'wait', '--for=delete', 'pod/elastic-operator-0', '--timeout=120s', timeout=150)
        for topology in ('standard', 'compact'):
            client_job(topology, 'business')
            data_ledger(topology)
    finally:
        primary = sys.exc_info()[1]
        try: restore_faults()
        except Exception as error:
            if primary is None: raise
            print('ELASTICSEARCH_FAULT_RESTORE_FAIL reason=' + type(error).__name__, file=sys.stderr)
    for topology in ('standard', 'compact'):
        verify_topology(topology); data_ledger(topology)
    print('ELASTICSEARCH_CASE_PASS id=ES-32', flush=True)


def unavailable_write_fault(topology, kind):
    c, k = selected_config(topology), api()
    ledger = data_ledger(topology)
    ns, name = c['elasticsearch_namespace'], c['elasticsearch_name']
    if kind == 'quorum':
        group, replicas = ('master', 1) if topology == 'standard' else ('data', 1)
        diagnostic = name+'-es-data-0'
    elif kind == 'shard-loss' and topology == 'standard':
        group, replicas, diagnostic = 'data', 0, name+'-es-master-0'
    else:
        raise ValueError('unsupported data safety fixture topology')
    target = name+'-es-'+group
    original = k.get('statefulset', target, ns)
    if not original or original['spec']['replicas'] != 3:
        raise ValueError('data safety fixture requires the original three-member group')
    fault_record('statefulset', 'elastic-operator', 'elastic-system', 1)
    try:
        k.run('-n', 'elastic-system', 'scale', 'statefulset/elastic-operator', '--replicas=0')
        k.run('-n', 'elastic-system', 'wait', '--for=delete', 'pod/elastic-operator-0', '--timeout=120s', timeout=150)
        fault_record('statefulset', target, ns, 3)
        k.run('-n', ns, 'scale', 'statefulset/'+target, '--replicas='+str(replicas))
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            remaining = [p for p in k.get('pods', namespace=ns)['items']
                if p['metadata'].get('labels', {}).get('elasticsearch.k8s.elastic.co/statefulset-name') == target]
            if len(remaining) == replicas: break
            print('ELASTICSEARCH_WAIT_HEARTBEAT fault='+kind+' topology='+topology, flush=True); time.sleep(3)
        else: raise ValueError('data safety fixture did not stop its exact declared members')
        pod = k.get('pod', diagnostic, ns)
        if not pod: raise ValueError('data safety fixture lacks its surviving diagnostic member')
        raw = client_job(topology, 'unavailable-write', fault_pod=pod, fault_kind=kind, fault_index=ledger['index'])
        measurements = [json.loads(line.removeprefix('ELASTICSEARCH_BUSINESS_MEASUREMENT ')) for line in raw.splitlines()
                        if line.startswith('ELASTICSEARCH_BUSINESS_MEASUREMENT ')]
        if len(measurements) != 1 or measurements[0].get('fault') != kind or measurements[0].get('write_http_status') != 503:
            raise ValueError('data safety client result lacks its explicit rejected-write evidence')
        identifier = measurements[0]['rejected_id']
    finally:
        primary = sys.exc_info()[1]
        try:
            restore_faults()
            for current in ('standard', 'compact'):
                verify_topology(current); data_ledger(current)
        except Exception as error:
            if primary is None: raise
            print('ELASTICSEARCH_DATA_SAFETY_RESTORE_FAIL reason='+type(error).__name__, file=sys.stderr)
    with load_es().connection(c, k) as request:
        result = request('POST', '/'+ledger['index']+'/_mget', {'ids': [identifier]})
        if len(result['docs']) != 1 or result['docs'][0].get('found') is not False:
            raise ValueError('rejected write unexpectedly appeared after the fault recovered')
    print('ELASTICSEARCH_CASE_PASS id=' + ('ES-33' if kind == 'quorum' else 'ES-34'), flush=True)
    print('ELASTICSEARCH_DATA_SAFETY_SCOPED_CLEAN_PASS topology='+topology+' fault='+kind, flush=True)


def faults():
    baseline()
    if list(STATE.glob('fault-*.json')): raise ValueError('fault residue must be recovered before another injection')
    for topology in ('standard', 'compact'): primary_pod_fault(topology)
    operator_fault()
    worker_fault()
    for topology in ('standard', 'compact'): unavailable_write_fault(topology, 'quorum')
    unavailable_write_fault('standard', 'shard-loss')
    print('ELASTICSEARCH_FAULTS_SCOPED_CLEAN_PASS', flush=True)


def maintenance_scale():
    topology, path = 'standard', STATE/'maintenance-scale.json'
    c, k, es = selected_config(topology), api(), load_es()
    if path.exists():
        record = json.loads(path.read_text())
        if record.get('phase') == 'complete':
            verify_topology(topology); data_ledger(topology)
            current = topology_state(topology)
            if any(current[key] != record['after'][key] for key in ('cr_uid', 'pvc_uids', 'credentials_uid', 'credentials_sha256')):
                raise ValueError('completed scale receipt identity differs')
            print('ELASTICSEARCH_CASE_PASS id=ES-36', flush=True)
            return
    else:
        if es.data_count(c) != 3 or len(c['elasticsearch_data_nodes']) != 4:
            raise ValueError('scale qualification requires three members with four eligible hosts')
        record = {'before': topology_state(topology)}; atomic(path, record)
    selected_config(topology, elasticsearch_data_count=4)
    product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
    c = verify_topology(topology); ledger = data_ledger(topology)
    now = topology_state(topology)
    if now['cr_uid'] != record['before']['cr_uid'] or any(now['pvc_uids'].get(n) != uid for n, uid in record['before']['pvc_uids'].items()):
        raise ValueError('scale-out changed original CR or PVC identities')
    deadline = time.monotonic()+300
    with es.connection(c, k) as request:
        while time.monotonic() < deadline:
            shards = request('GET', '/_cat/shards/'+ledger['index']+'?format=json')
            if any(row['node'] == c['elasticsearch_name']+'-es-data-3' and row['state'] == 'STARTED' for row in shards): break
            print('ELASTICSEARCH_WAIT_HEARTBEAT maintenance=scale-shard-movement', flush=True); time.sleep(5)
        else: raise ValueError('new data member did not receive a real confirmed-index shard')
    try:
        selected_config(topology, elasticsearch_data_count=3)
        product_refusal('scale-in deletes ECK PVCs', 'scale-in')
    finally:
        selected_config(topology, elasticsearch_data_count=4)
    verify_topology(topology); data_ledger(topology)
    record['after'] = topology_state(topology); record['phase'] = 'complete'; atomic(path, record)
    print('ELASTICSEARCH_CASE_PASS id=ES-36', flush=True)


def maintenance_drain():
    k = api()
    for topology in ('standard', 'compact'): verify_topology(topology); data_ledger(topology)
    cleanup_namespaces()
    path = STATE/'maintenance-drain.json'
    if path.exists():
        record = json.loads(path.read_text())
        if record.get('phase') != 'complete' or record['cluster_uid'] != baseline()['cluster_uid'] or list(STATE.glob('fault-*.json')):
            raise ValueError('drain receipt no longer belongs to the healthy baseline')
        node = k.get('node', record['node'])
        if not node or node['metadata']['uid'] != record['node_uid'] or node.get('spec', {}).get('unschedulable'):
            raise ValueError('drain receipt node identity or scheduling differs')
        for old in record['pods']:
            pod = k.get('pod', old['name'], old['namespace'])
            if not pod or volume_binding(pod, attached_node=pod['spec']['nodeName']) != old['binding']:
                raise ValueError('completed drain receipt original volume identity differs')
        print('ELASTICSEARCH_CASE_PASS id=ES-35', flush=True)
        return
    c = selected_config('standard')
    if load_es().data_count(c) != 3: raise ValueError('planned drain needs its original spare data host')
    pods = [p for p in k.get('pods')['items'] if p['metadata'].get('labels', {}).get('elasticsearch.k8s.elastic.co/cluster-name') == 'search']
    target = next(p['spec']['nodeName'] for p in pods if p['metadata']['namespace'] == 'es-standard' and p['spec']['nodeName'] in NODES[3:])
    node = k.get('node', target)
    if node.get('spec', {}).get('unschedulable') or node.get('spec', {}).get('taints', []):
        raise ValueError('planned drain requires the original schedulable untainted worker')
    affected = [p for p in pods if p['spec']['nodeName'] == target]
    for namespace in {p['metadata']['namespace'] for p in affected}:
        obj = k.get('elasticsearch', 'search', namespace)
        budgets = [p for p in k.get('pdb', namespace=namespace)['items'] if any(
            r.get('uid') == obj['metadata']['uid'] for r in p['metadata'].get('ownerReferences', []))]
        if len(budgets) != 1 or budgets[0].get('status', {}).get('observedGeneration') != budgets[0]['metadata']['generation'] or budgets[0]['status'].get('disruptionsAllowed', 0) < 1:
            raise ValueError('planned drain lacks the current ECK-owned disruption budget')
    record = {'cluster_uid': baseline()['cluster_uid'], 'node': target, 'node_uid': node['metadata']['uid'], 'taints': [],
              'cordon_intent': True, 'pods': [{'name': p['metadata']['name'], 'namespace': p['metadata']['namespace'], 'uid': p['metadata']['uid'],
                                           'binding': volume_binding(p, attached_node=target)} for p in affected]}
    fault = STATE/('fault-worker-'+target+'.json')
    if fault.exists(): raise ValueError('planned drain refuses an existing worker journal')
    atomic(fault, record)
    try:
        # Use the eviction API and all existing budgets. No force, bypass or
        # infrastructure policy change is permitted to make this gate succeed.
        k.run('drain', target, '--ignore-daemonsets', '--delete-emptydir-data', '--timeout=900s', timeout=960)
        deadline = time.monotonic()+600
        while time.monotonic() < deadline:
            migrated = []
            for old in record['pods']:
                pod = k.get('pod', old['name'], old['namespace'])
                if not pod or pod['metadata']['uid'] == old['uid'] or pod.get('spec', {}).get('nodeName') in (None, target): break
                if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in pod.get('status', {}).get('conditions', [])): break
                if volume_binding(pod, attached_node=pod['spec']['nodeName']) != old['binding']:
                    raise ValueError('planned drain replaced the original PVC/PV/native volume')
                migrated.append(pod['metadata']['name'])
            if len(migrated) == len(record['pods']): break
            print('ELASTICSEARCH_WAIT_HEARTBEAT maintenance=drain-migration', flush=True); time.sleep(5)
        else: raise ValueError('evicted members did not recover on a different worker')
        for topology in ('standard', 'compact'): verify_topology(topology); data_ledger(topology); client_job(topology, 'business')
    finally:
        primary = sys.exc_info()[1]
        try: restore_worker(fault)
        except Exception as error:
            if primary is None: raise
            print('ELASTICSEARCH_DRAIN_RESTORE_FAIL reason='+type(error).__name__, file=sys.stderr)
    for topology in ('standard', 'compact'): verify_topology(topology); data_ledger(topology)
    record['phase'] = 'complete'; atomic(path, record)
    print('ELASTICSEARCH_CASE_PASS id=ES-35', flush=True)


def data_filesystems(topology):
    c, k, es = selected_config(topology), api(), load_es()
    with es.connection(c, k) as request:
        nodes = request('GET', '/_nodes/stats/fs')['nodes']
    result = {}
    for node in nodes.values():
        if not node['name'].startswith(c['elasticsearch_name']+'-es-data-'): continue
        paths = node.get('fs', {}).get('data', [])
        if len(paths) != 1 or paths[0].get('total_in_bytes', 0) <= 0:
            raise ValueError('data node lacks its actual single data filesystem measurement')
        result[node['name']] = paths[0]['total_in_bytes']
    if len(result) != es.data_count(c): raise ValueError('data filesystem measurements are incomplete')
    return result


def expanded_volumes(topology, before, size):
    c, k, es = selected_config(topology), api(), load_es()
    current = topology_state(topology)
    if current['pvc_uids'] != before['identity']['pvc_uids'] or current['cr_uid'] != before['identity']['cr_uid']:
        raise ValueError('expansion changed original PVC or Elasticsearch identity')
    required = es.quantity(size)
    for claim in k.get('pvc', namespace=c['elasticsearch_namespace'])['items']:
        if '-es-data-' not in claim['metadata']['name']: continue
        if es.quantity(claim['spec']['resources']['requests']['storage']) < required or es.quantity(claim.get('status', {}).get('capacity', {}).get('storage', '0')) < required:
            return False
        pv = k.get('pv', claim['spec']['volumeName'])
        if es.quantity(pv['spec']['capacity']['storage']) < required: return False
        native = k.get('volumes.longhorn.io', pv['spec']['csi']['volumeHandle'], 'longhorn-system')
        if int(native['spec']['size']) < required or native.get('status', {}).get('expansionRequired'):
            return False
    filesystems = data_filesystems(topology)
    if set(filesystems) != set(before['filesystems']): raise ValueError('expansion changed the measured data member set')
    return all(total > before['filesystems'][name] and total >= required*.9 for name, total in filesystems.items())


def maintenance_expansion():
    for topology in ('standard', 'compact'):
        c, path = selected_config(topology), STATE/('maintenance-expansion-'+topology+'.json')
        if path.exists(): record = json.loads(path.read_text())
        else:
            if c['elasticsearch_data_pvc_size'] != '4Gi': raise ValueError('expansion qualification requires its original 4Gi data volumes')
            record = {'identity': topology_state(topology), 'filesystems': data_filesystems(topology)}; atomic(path, record)
        if record.get('phase') == 'complete':
            if c['elasticsearch_data_pvc_size'] != '6Gi' or not expanded_volumes(topology, record, '6Gi'):
                raise ValueError('completed expansion receipt no longer matches the retained volumes')
            verify_topology(topology); data_ledger(topology)
            print('ELASTICSEARCH_EXPANSION_REUSED topology=' + topology + ' requested=6Gi', flush=True)
            continue
        selected_config(topology, elasticsearch_data_pvc_size='6Gi')
        product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
        deadline = time.monotonic()+600
        while time.monotonic() < deadline:
            if expanded_volumes(topology, record, '6Gi'): break
            print('ELASTICSEARCH_WAIT_HEARTBEAT maintenance=PVC-native-filesystem-expansion topology='+topology, flush=True); time.sleep(5)
        else: raise ValueError('PVC/PV/native volume/data filesystem did not all expand')
        try:
            selected_config(topology, elasticsearch_data_pvc_size='4Gi')
            product_refusal('PVC class changes and shrink are forbidden', 'PVC-shrink-'+topology)
        finally:
            selected_config(topology, elasticsearch_data_pvc_size='6Gi')
        verify_topology(topology); data_ledger(topology)
        record['after_filesystems'] = data_filesystems(topology); record['phase'] = 'complete'; atomic(path, record)
        print('ELASTICSEARCH_EXPANSION_MEASUREMENT '+json.dumps({'topology': topology, 'before': record['filesystems'], 'after': record['after_filesystems'], 'requested': '6Gi'}, sort_keys=True), flush=True)
    print('ELASTICSEARCH_CASE_PASS id=ES-37', flush=True)


WATERMARK_KEYS = ('cluster.routing.allocation.disk.watermark.low', 'cluster.routing.allocation.disk.watermark.high',
    'cluster.routing.allocation.disk.watermark.flood_stage', 'cluster.routing.allocation.disk.watermark.low.max_headroom',
    'cluster.routing.allocation.disk.watermark.high.max_headroom', 'cluster.routing.allocation.disk.watermark.flood_stage.max_headroom',
    'cluster.info.update.interval')


def restore_watermarks(path):
    record, k, es = json.loads(path.read_text()), api(), load_es()
    c = selected_config(record['topology'])
    obj = k.get('elasticsearch', c['elasticsearch_name'], c['elasticsearch_namespace'])
    if not obj or obj['metadata']['uid'] != record['cr_uid']: raise ValueError('watermark recovery cluster identity changed')
    with es.connection(c, k) as request:
        request('PUT', '/_cluster/settings', record['settings'])
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            settings = request('GET', '/_cluster/settings?flat_settings=true')
            if any(settings.get(group, {}).get(key) != value for group, values in record['settings'].items() for key, value in values.items()):
                raise ValueError('watermark settings did not restore exactly')
            index_settings = request('GET', '/'+record['index']+'/_settings?flat_settings=true')[record['index']]['settings']
            if index_settings['index.uuid'] != record['index_uuid']: raise ValueError('watermark recovery index identity changed')
            if str(index_settings.get('index.blocks.read_only_allow_delete')).lower() != 'true': break
            print('ELASTICSEARCH_WAIT_HEARTBEAT recovery=automatic-flood-block-release', flush=True); time.sleep(5)
        else: raise ValueError('watermark block was not automatically released after capacity threshold restoration')
        if record.get('rejected_id'):
            documents = request('POST', '/'+record['index']+'/_mget', {'ids': [record['rejected_id']]})['docs']
            if len(documents) != 1 or documents[0].get('found') is not False: raise ValueError('watermark rejected write appeared after recovery')
    data_ledger(record['topology'])
    path.unlink()


def maintenance_watermarks():
    k, es = api(), load_es()
    for topology in ('standard', 'compact'):
        c = selected_config(topology); ledger = data_ledger(topology)
        path = STATE/('fault-watermarks-'+topology+'.json')
        if path.exists(): raise ValueError('watermark scenario has an unrecovered journal')
        with es.connection(c, k) as request:
            current = request('GET', '/_cluster/settings?flat_settings=true')
            nodes = request('GET', '/_nodes/stats/fs')['nodes']
            free = max(p['available_in_bytes'] for n in nodes.values() if n['name'].startswith(c['elasticsearch_name']+'-es-data-') for p in n['fs']['data'])
            settings = {group: {key: current.get(group, {}).get(key) for key in WATERMARK_KEYS} for group in ('persistent', 'transient')}
            record = {'topology': topology, 'settings': settings, 'index': ledger['index'], 'index_uuid': ledger['index_uuid'],
                      'cr_uid': k.get('elasticsearch', c['elasticsearch_name'], c['elasticsearch_namespace'])['metadata']['uid']}
            atomic(path, record)
            try:
                thresholds = {key: str(free+increment*1024**2)+'b' for key, increment in zip(WATERMARK_KEYS[:3], (512, 256, 128))}
                # The pinned MaxHeadroomValidator rejects an explicit -1.
                # Remove overrides: explicit byte watermarks then select the
                # upstream default (disabled headroom) without invalid input.
                thresholds.update({key: None for key in WATERMARK_KEYS[3:6]}); thresholds[WATERMARK_KEYS[-1]] = '10s'
                request('PUT', '/_cluster/settings', {'persistent': thresholds, 'transient': {key: None for key in WATERMARK_KEYS}})
                deadline = time.monotonic()+180
                while time.monotonic() < deadline:
                    state = request('GET', '/'+ledger['index']+'/_settings?flat_settings=true')[ledger['index']]['settings']
                    if str(state.get('index.blocks.read_only_allow_delete')).lower() == 'true': break
                    print('ELASTICSEARCH_WAIT_HEARTBEAT maintenance=automatic-flood-block topology='+topology, flush=True); time.sleep(5)
                else: raise ValueError('measured-free-space thresholds did not trigger the real flood-stage protection')
                pod = k.get('pod', c['elasticsearch_name']+'-es-data-0', c['elasticsearch_namespace'])
                raw = client_job(topology, 'unavailable-write', fault_pod=pod, fault_kind='flood-watermark', fault_index=ledger['index'])
                samples = [json.loads(line.removeprefix('ELASTICSEARCH_BUSINESS_MEASUREMENT ')) for line in raw.splitlines() if line.startswith('ELASTICSEARCH_BUSINESS_MEASUREMENT ')]
                if len(samples) != 1 or samples[0].get('write_http_status') != 429 or samples[0].get('write_error_type') != 'cluster_block_exception':
                    raise ValueError('watermark protection lacks an explicit HTTP 429 cluster-block refusal')
                record['rejected_id'] = samples[0]['rejected_id']; atomic(path, record)
                print('ELASTICSEARCH_WATERMARK_MEASUREMENT '+json.dumps({'topology': topology, 'measured_free_bytes': free, 'thresholds': thresholds,
                    'fixture': 'thresholds above measured free space; no physical disk fill'}, sort_keys=True), flush=True)
            finally:
                primary = sys.exc_info()[1]
                try: restore_watermarks(path)
                except Exception as error:
                    if primary is None: raise
                    print('ELASTICSEARCH_WATERMARK_RESTORE_FAIL reason='+type(error).__name__, file=sys.stderr)
        client_job(topology, 'business'); verify_topology(topology)
    print('ELASTICSEARCH_CASE_PASS id=ES-38', flush=True)


def maintenance():
    baseline()
    for topology in ('standard', 'compact'): release_identity_ownership(topology)
    maintenance_drain()
    maintenance_scale()
    maintenance_expansion()
    maintenance_watermarks()
    print('ELASTICSEARCH_MAINTENANCE_SCOPED_CLEAN_PASS', flush=True)


def s3_fixture(action):
    execute([BASE/'.venv/bin/python', BASE/'tests/helpers/elasticsearch-s3.py', action], capture=False, timeout=900)


def dr_fixture(action, topology='compact'):
    execute([BASE/'.venv/bin/python', BASE/'tests/helpers/elasticsearch-dr.py', action, '--topology', topology], capture=False, timeout=14400)


def configure_s3():
    s3_fixture('prepare')
    store = json.loads((STATE/'s3.json').read_text())
    ca = (STATE/'s3/certs/public.crt').read_text()
    k = api()
    for topology in ('standard', 'compact'):
        c = selected_config(topology)
        ns = c['elasticsearch_namespace']
        for kind, name in (('secret', 'es-gate-s3'), ('configmap', 'es-gate-s3-ca')):
            obj = k.get(kind, name, ns)
            if obj and not all(obj['metadata'].get('labels', {}).get(key) == value for key, value in LABELS.items()):
                raise ValueError('foreign S3 fixture identity')
        k.apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'es-gate-s3', 'namespace': ns, 'labels': LABELS},
                  'type': 'Opaque', 'stringData': {'s3.client.default.access_key': store['access_key'], 's3.client.default.secret_key': store['secret_key']}},
                 {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'es-gate-s3-ca', 'namespace': ns, 'labels': LABELS}, 'data': {'ca.crt': ca}}])
        # Match the pinned upstream MinIO analysis fixture (#135565). This
        # backend cannot complete copy-before-upload contention; retain all
        # other analysis, snapshot and independent restore checks.
        selected_config(topology, elasticsearch_s3_secret='es-gate-s3', elasticsearch_s3_ca_configmap='es-gate-s3-ca',
                        elasticsearch_repository_analysis_copy_during_write_contention=False)


def repository_request(topology, store):
    return {'id': 'gate-s3-repository', 'type': 'repository', 'name': 'gate-archive', 'endpoint': store['endpoint'],
            'bucket': store['bucket'], 'base_path': store['owner']+'/'+topology, 'region': 'us-east-1', 'path_style_access': True}


def verify_minio_analysis(c):
    """Check the declared official backend exception on every live member."""
    es = load_es()
    with es.connection(c, api()) as request:
        nodes = request('GET', '/_nodes/settings?flat_settings=true')['nodes']
        key = 'repositories.blobstore.testkit.analyze.copy_during_write_contention'
        if len(nodes) != es.member_count(c) or any(
                str(node.get('settings', {}).get(key)).lower() != 'false' for node in nodes.values()):
            raise ValueError('MinIO compatibility did not reach every ES member')
    print('ELASTICSEARCH_MINIO_ANALYSIS_COMPATIBILITY_PASS topology='+c['elasticsearch_topology']+
          ' upstream=135565 copy_before_upload=false other_analysis=required', flush=True)


def wait_repository(topology):
    c, k, es = selected_config(topology), api(), load_es()
    deadline = time.monotonic()+180
    while time.monotonic() < deadline:
        try:
            with es.connection(c, k) as request:
                if request('POST', '/_snapshot/gate-archive/_verify').get('nodes'): return
        except ValueError:
            # ECK reloads watched secureSettings asynchronously. The product
            # snapshot after this diagnostic remains the business acceptance.
            pass
        print('ELASTICSEARCH_WAIT_HEARTBEAT operation=s3-secure-settings topology='+topology, flush=True); time.sleep(5)
    raise ValueError('S3 credentials did not converge on the registered repository')


def restore_s3_rotation():
    path, k = STATE/'fault-s3-rotation.json', api()
    record = json.loads(path.read_text())
    for topology, original in record['secrets'].items():
        c = selected_config(topology)
        obj = k.get('secret', c['elasticsearch_s3_secret'], c['elasticsearch_namespace'])
        if not obj or obj['metadata']['uid'] != original['uid']:
            raise ValueError('S3 rotation recovery refuses replaced credential Secret')
    s3_fixture('rotation-restore')
    for topology, original in record['secrets'].items():
        c = selected_config(topology)
        k.apply([{'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque', 'metadata': {
            'name': c['elasticsearch_s3_secret'], 'namespace': c['elasticsearch_namespace'], 'labels': LABELS}, 'data': original['data']}])
        selected_config(topology, elasticsearch_requests=[{'id': 's3-'+record['token']+'-restore', 'type': 'reload-secure-settings'}])
        product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
        wait_repository(topology); data_ledger(topology)
        snapshot = record.get('snapshots', {}).get(topology)
        if snapshot:
            with load_es().connection(c, k) as request:
                snapshots = request('GET', '/_snapshot/gate-archive/'+snapshot+'?ignore_unavailable=true').get('snapshots', [])
                if snapshots:
                    if len(snapshots) != 1 or snapshots[0].get('metadata', {}).get('kubeauto_change_id') != snapshot:
                        raise ValueError('S3 rotation cleanup refuses foreign snapshot identity')
                    request('DELETE', '/_snapshot/gate-archive/'+snapshot)
                if request('GET', '/_snapshot/gate-archive/'+snapshot+'?ignore_unavailable=true').get('snapshots'):
                    raise ValueError('S3 rotation snapshot cleanup is incomplete')
        selected_config(topology, elasticsearch_requests=[])
    path.unlink()


def s3_rotation():
    configure_s3()
    k, es = api(), load_es()
    store = json.loads((STATE/'s3.json').read_text())
    for topology in ('standard', 'compact'):
        selected_config(topology, elasticsearch_requests=[repository_request(topology, store)])
        product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
        selected_config(topology, elasticsearch_requests=[])
    record = {'token': secrets.token_hex(8), 'secrets': {}}
    for topology in ('standard', 'compact'):
        c = selected_config(topology)
        obj = k.get('secret', c['elasticsearch_s3_secret'], c['elasticsearch_namespace'])
        record['secrets'][topology] = {'uid': obj['metadata']['uid'], 'data': obj['data']}
    path = STATE/'fault-s3-rotation.json'
    if path.exists(): raise ValueError('S3 rotation has an unrecovered journal')
    atomic(path, record)
    try:
        s3_fixture('rotation-prepare')
        replacement = json.loads((STATE/'s3.json').read_text())['rotation']
        for topology in ('standard', 'compact'):
            c = selected_config(topology)
            obj = k.get('secret', c['elasticsearch_s3_secret'], c['elasticsearch_namespace'])
            if obj['metadata']['uid'] != record['secrets'][topology]['uid']: raise ValueError('S3 credential Secret was replaced')
            k.apply([{'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque', 'metadata': {
                'name': c['elasticsearch_s3_secret'], 'namespace': c['elasticsearch_namespace'], 'labels': LABELS},
                'stringData': {'s3.client.default.access_key': replacement['access_key'], 's3.client.default.secret_key': replacement['secret_key']}}])
            selected_config(topology, elasticsearch_requests=[{'id': 's3-'+record['token']+'-reload', 'type': 'reload-secure-settings'}])
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
        s3_fixture('rotation-disable-old')
        for topology in ('standard', 'compact'):
            wait_repository(topology)
            ledger = data_ledger(topology)
            snapshot = 'rotation-'+record['token']
            record.setdefault('snapshots', {})[topology] = snapshot; atomic(path, record)
            c = selected_config(topology, elasticsearch_requests=[{'id': snapshot, 'type': 'snapshot', 'repository': 'gate-archive',
                                    'snapshot': snapshot, 'indices': [ledger['index']]}])
            product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
            with es.connection(c, k) as request:
                result = request('GET', '/_snapshot/gate-archive/'+snapshot)['snapshots'][0]
                if result.get('state') != 'SUCCESS' or result.get('shards', {}).get('successful', 0) < 1 or result['shards']['failed']:
                    raise ValueError('rotated S3 credentials lack a real successful snapshot')
                request('DELETE', '/_snapshot/gate-archive/'+snapshot)
    finally:
        primary = sys.exc_info()[1]
        try: restore_s3_rotation()
        except Exception as error:
            if primary is None: raise
            print('ELASTICSEARCH_S3_ROTATION_RESTORE_FAIL reason='+type(error).__name__, file=sys.stderr)
    print('ELASTICSEARCH_CASE_PASS id=ES-28', flush=True)
    print('ELASTICSEARCH_S3_ROTATION_SCOPED_CLEAN_PASS credentials=restored old-key=verified', flush=True)


def backup_source():
    baseline()
    configure_s3()
    store, k = json.loads((STATE/'s3.json').read_text()), api()
    for topology in ('standard', 'compact'):
        c = selected_config(topology)
        ns = c['elasticsearch_namespace']
        ledger = data_ledger(topology)
        requests = [repository_request(topology, store),
                    {'id': 'gate-backup', 'type': 'snapshot', 'repository': 'gate-archive', 'snapshot': 'gate-confirmed', 'indices': [ledger['index']]},
                    {'id': 'gate-slm', 'type': 'slm', 'name': 'gate-policy', 'policy': {'schedule': '0 0 0 * * ?', 'name': '<gate-slm-{now/d}>',
                        'repository': 'gate-archive', 'config': {'indices': [ledger['index']], 'include_global_state': False, 'feature_states': ['none']},
                        'retention': {'expire_after': '1d', 'min_count': 1, 'max_count': 2}}}]
        c = selected_config(topology, elasticsearch_requests=requests)
        product('setup', NAME, '07', '--tags', 'elasticsearch', timeout=5400)
        verify_topology(topology); data_ledger(topology)
        verify_minio_analysis(c)
        with load_es().connection(c, k) as request:
            repo = request('GET', '/_snapshot/gate-archive')['gate-archive']
            if repo['type'] != 's3' or str(repo['settings'].get('readonly')).lower() != 'false': raise ValueError('source repository is not the only configured writer')
            snapshot = request('GET', '/_snapshot/gate-archive/gate-confirmed')['snapshots'][0]
            if snapshot['state'] != 'SUCCESS' or not snapshot.get('indices') or snapshot['shards']['total'] <= 0 or snapshot['shards']['failed']:
                raise ValueError('backup has no nonzero successful shard evidence')
            generated = request('PUT', '/_slm/policy/gate-policy/_execute')['snapshot_name']
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                matches = request('GET', '/_snapshot/gate-archive/' + generated + '?ignore_unavailable=true').get('snapshots', [])
                if matches and matches[0]['state'] == 'SUCCESS': break
                if matches and matches[0]['state'] not in ('IN_PROGRESS', 'STARTED'): raise ValueError('SLM execution failed')
                print('ELASTICSEARCH_WAIT_HEARTBEAT operation=SLM topology=' + topology, flush=True); time.sleep(5)
            else: raise ValueError('SLM snapshot completion timed out')
            status = request('GET', '/_slm/policy/gate-policy')['gate-policy']
            if status['policy']['retention'] != requests[-1]['policy']['retention']: raise ValueError('SLM retention differs from product input')
            created = [generated]
            for _ in range(2):
                candidate = request('PUT', '/_slm/policy/gate-policy/_execute')['snapshot_name']
                created.append(candidate)
                deadline = time.monotonic()+600
                while time.monotonic()<deadline:
                    matches=request('GET','/_snapshot/gate-archive/'+candidate+'?ignore_unavailable=true').get('snapshots',[])
                    if matches and matches[0]['state']=='SUCCESS': break
                    if matches and matches[0]['state'] not in ('IN_PROGRESS','STARTED'):raise ValueError('SLM retention fixture snapshot failed')
                    print('ELASTICSEARCH_WAIT_HEARTBEAT operation=SLM-retention-fixture topology='+topology,flush=True);time.sleep(5)
                else:raise ValueError('SLM retention fixture snapshot did not complete')
            request('POST','/_slm/_execute_retention')
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                retained=request('GET','/_snapshot/gate-archive/'+','.join(created)+'?ignore_unavailable=true')['snapshots']
                names={snapshot['snapshot'] for snapshot in retained}
                if names==set(created[-2:]) and all(snapshot['state']=='SUCCESS' for snapshot in retained):break
                print('ELASTICSEARCH_WAIT_HEARTBEAT operation=SLM-real-retention topology='+topology,flush=True);time.sleep(5)
            else:raise ValueError('SLM max_count retention did not remove only its oldest snapshot')
        atomic(STATE/('backup-'+topology+'.json'), {'repository': 'gate-archive', 'snapshot': 'gate-confirmed', 'snapshot_uuid': snapshot['uuid'],
                'source_cluster_uuid': k.get('configmap', c['elasticsearch_name']+'-identity', ns)['data']['cluster_uuid'],
                'index': ledger['index'], 'documents': ledger['documents'], 'sha256': ledger['sha256'], 'end_time_ms':snapshot['end_time_in_millis'],
                'slm_snapshots_retained':created[-2:]})
        selected_config(topology, elasticsearch_requests=[])
    print('ELASTICSEARCH_CASE_PASS id=ES-39', flush=True)
    # ES-40/41/42 require negative, independent restore and feature-state
    # evidence; successful source snapshots cannot sign those cases.


def restore_s3_negative():
    path=STATE/'fault-s3-negative.json';record=json.loads(path.read_text())
    c,k=selected_config('standard'),api();ns=c['elasticsearch_namespace']
    for kind,key,name in (('secret','secret',c['elasticsearch_s3_secret']),('configmap','ca',c['elasticsearch_s3_ca_configmap'])):
        obj=k.get(kind,name,ns)
        if not obj or obj['metadata']['uid']!=record[key]['uid']:raise ValueError('S3 negative recovery refuses replaced trust or credential identity')
    for kind,key,name in (('Secret','secret',c['elasticsearch_s3_secret']),('ConfigMap','ca',c['elasticsearch_s3_ca_configmap'])):
        k.apply([{'apiVersion':'v1','kind':kind,'metadata':{'name':name,'namespace':ns,'labels':LABELS},'data':record[key]['data']}])
    selected_config('standard',elasticsearch_requests=[])
    product('setup',NAME,'07','--tags','elasticsearch',timeout=5400)
    wait_repository('standard');data_ledger('standard')
    s3_fixture('negative-restore')
    # Rejected requests retain their first private failure log. Their running
    # product journals are removed only after matching the planned fingerprint
    # and data-cluster identity, so they cannot poison a later recovery run.
    for req in record['requests']:
        name=c['elasticsearch_name']+'-request-'+req['id'];obj=k.get('configmap',name,ns)
        if obj:
            fingerprint=hashlib.sha256(json.dumps(req,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if not load_es().owned_resource(c,obj) or obj.get('data',{}).get('fingerprint')!=fingerprint or obj['data'].get('cluster_uuid')!=record['cluster_uuid']:
                raise ValueError('S3 negative cleanup refuses a foreign operation journal')
            k.run('-n',ns,'delete','configmap',name,'--wait=true')
    path.unlink()


def backup_refusal():
    baseline()
    c,k=selected_config('standard'),api();ns=c['elasticsearch_namespace']
    store=json.loads((STATE/'s3.json').read_text());path=STATE/'fault-s3-negative.json'
    if path.exists():raise ValueError('S3 negative fixture has an unrecovered journal')
    for defect in ('credentials','ca','delete-capability'):
        data_ledger('standard')
        record={'requests':[],'cluster_uuid':k.get('configmap',c['elasticsearch_name']+'-identity',ns)['data']['cluster_uuid']}
        for kind,key,name in (('secret','secret',c['elasticsearch_s3_secret']),('configmap','ca',c['elasticsearch_s3_ca_configmap'])):
            obj=k.get(kind,name,ns);record[key]={'uid':obj['metadata']['uid'],'data':obj['data']}
        atomic(path,record)
        try:
            if defect=='ca':
                wrong_ca=base64.b64decode(certificate_data(c)['ca.crt']).decode()
                # The same CA is well formed yet cannot authenticate this
                # independent backend. TLS errors are not credential evidence.
                import urllib.request
                try:urllib.request.urlopen(store['endpoint']+'/minio/health/live',context=ssl.create_default_context(cadata=wrong_ca),timeout=15)
                except urllib.error.URLError as error:
                    if not isinstance(error.reason,ssl.SSLCertVerificationError):raise
                else:raise ValueError('negative CA unexpectedly trusted the real backend')
                k.apply([{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':c['elasticsearch_s3_ca_configmap'],'namespace':ns,'labels':LABELS},'data':{'ca.crt':wrong_ca}}])
            else:
                if defect=='delete-capability':
                    s3_fixture('negative-prepare');account=json.loads((STATE/'s3.json').read_text())['negative']
                    if account.get('phase')!='ready':raise ValueError('backend capability fixture was not positively qualified')
                else:account={'access_key':'invalid'+secrets.token_hex(8),'secret_key':secrets.token_urlsafe(32)}
                k.apply([{'apiVersion':'v1','kind':'Secret','type':'Opaque','metadata':{'name':c['elasticsearch_s3_secret'],'namespace':ns,'labels':LABELS},
                    'stringData':{'s3.client.default.access_key':account['access_key'],'s3.client.default.secret_key':account['secret_key']}}])
            # ECK synchronizes secureSettings asynchronously. Converge the
            # product spec first, then wait for the specific server refusal.
            selected_config('standard',elasticsearch_requests=[])
            product('setup',NAME,'07','--tags','elasticsearch',timeout=5400)
            if defect!='ca':
                reload_request={'id':'negative-reload-'+secrets.token_hex(4),'type':'reload-secure-settings'}
                record['requests'].append(reload_request);atomic(path,record)
                selected_config('standard',elasticsearch_requests=[reload_request])
                deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    product('setup',NAME,'07','--tags','elasticsearch',timeout=5400)
                    with load_es().connection(c,k) as request:
                        try:request('POST','/_snapshot/gate-archive/_verify')
                        except ValueError as error:
                            if 'repository_verification_exception' in str(error):break
                            raise
                    print('ELASTICSEARCH_WAIT_HEARTBEAT backup=credential-rejection defect='+defect,flush=True);time.sleep(5)
                else:raise ValueError('invalid backend identity did not replace the previous valid credentials')
            req=repository_request('standard',store);req['id']='negative-'+defect+'-'+secrets.token_hex(4)
            record['requests'].append(req);atomic(path,record)
            selected_config('standard',elasticsearch_requests=[req])
            product_refusal('repository_verification_exception','s3-'+defect)
        finally:
            primary=sys.exc_info()[1]
            try:restore_s3_negative()
            except Exception as error:
                if primary is None:raise
                print('ELASTICSEARCH_S3_NEGATIVE_RESTORE_FAIL reason='+type(error).__name__,file=sys.stderr)
    print('ELASTICSEARCH_CASE_PASS id=ES-40',flush=True)
    print('ELASTICSEARCH_BACKUP_REFUSAL_SCOPED_CLEAN_PASS credentials=restored trust=restored backend_identity=removed',flush=True)


def backup_restore():
    baseline()
    for topology in ('standard','compact'):
        if not (STATE/('backup-'+topology+'.json')).exists():raise ValueError('isolated restore requires current source snapshot evidence')
    if not (STATE/'dr/ownership.json').exists(): dr_fixture('bootstrap')
    for topology in ('standard','compact'):
        dr_fixture('install',topology)
        dr_fixture('restore',topology)
        verify_topology(topology);data_ledger(topology)
    print('ELASTICSEARCH_CASE_PASS id=ES-41',flush=True)
    print('ELASTICSEARCH_BACKUP_RESTORE_SCOPED_CLEAN_PASS target=independent source=unchanged',flush=True)


def dr_bootstrap():
    # Qualify the composed handoff through the existing focused entry point.
    # The complete run starts the same independent target helper directly;
    # it does not recursively enter this qualification branch.
    baseline()
    spec=importlib.util.spec_from_file_location('es_parallel_platform',BASE/'tests/helpers/elasticsearch-full.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.qualify_platform()


def dr():
    baseline()
    spec=importlib.util.spec_from_file_location('es_dr_cutover',BASE/'tests/helpers/elasticsearch-dr.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.disaster(sys.modules[__name__])


def backup_features():
    baseline()
    spec=importlib.util.spec_from_file_location('es_dr_features',BASE/'tests/helpers/elasticsearch-dr.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.features(sys.modules[__name__])
    # Return the shared independent target to the declared DR prerequisite.
    # Feature recovery evidence binds its earlier UUID, not this new target.
    dr_fixture('install','compact');dr_fixture('restore','compact')


def backup():
    backup_source()
    backup_refusal()
    backup_restore()
    backup_features()
    print('ELASTICSEARCH_BACKUP_SCOPED_CLEAN_PASS source=healthy target=independent credentials=restored',flush=True)


def upgrade(*, platform_only=False):
    baseline()
    spec=importlib.util.spec_from_file_location('es_dr_upgrade',BASE/'tests/helpers/elasticsearch-dr.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.upgrade(sys.modules[__name__],platform_only=platform_only)


def upgrade_platform():
    upgrade(platform_only=True)


def coexistence(action='run'):
    spec=importlib.util.spec_from_file_location('es_coexistence',BASE/'tests/helpers/elasticsearch-coexistence.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    getattr(module,action)(sys.modules[__name__])


def coexistence_isolation():
    coexistence('isolation_repair')


def coexistence_restores():
    coexistence('restores_repair')


def stream_command(argv, path, environment, timeout):
    """Keep the child transcript durable while streaming and bounding its tree."""
    with open(path,'x',opener=lambda p,flags:os.open(p,flags,0o600)) as output:
        process=subprocess.Popen([str(a) for a in argv],stdout=output,stderr=subprocess.STDOUT,
                                 env=environment,start_new_session=True)
        deadline=time.monotonic()+timeout;heartbeat=time.monotonic()
        try:
            with path.open() as reader:
                while True:
                    chunk=reader.read()
                    if chunk:print(chunk,end='',flush=True)
                    status=process.poll()
                    if status is not None:
                        chunk=reader.read()
                        if chunk:print(chunk,end='',flush=True)
                        return status
                    if time.monotonic()>=deadline:raise subprocess.TimeoutExpired(argv,timeout)
                    if time.monotonic()-heartbeat>=30:
                        print('ELASTICSEARCH_WAIT_HEARTBEAT logging=true transcript='+str(path),flush=True)
                        heartbeat=time.monotonic()
                    time.sleep(1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=20)


def logging_inputs():
    # These inputs belong to the route's business transcript. Kafka retirement
    # runs afterwards and has its own current cleanup/verification boundary.
    names=('tests/helpers/logging-regression.sh','tests/helpers/logging-cleanup.sh',
           'tests/helpers/logging-artifact-gate.sh','tests/helpers/run-durable-gate.sh','tests/evidence/elasticsearch-images-20261103.json')
    return {name:hashlib.sha256((BASE/name).read_bytes()).hexdigest() for name in names}


def verify_logging_route(receipt, inputs):
    recorded=dict(receipt['inputs'])
    recorded.pop('tests/helpers/kafka-cleanup.sh',None)  # legacy overbinding
    if recorded!=inputs:raise ValueError('completed logging route inputs changed')
    stem=Path(receipt['durable']);log=Path(receipt['log'])
    for suffix in ('.exit','.finalized'):
        if Path(str(stem)+suffix).read_text().strip()!='0':raise ValueError('logging route has no successful durable fence')
    raw=log.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=receipt['sha256']:raise ValueError('logging transcript identity changed')
    lines=raw.decode().splitlines()
    if 'LOGGING_FULL_GATE_PASS' not in lines or any('[FAIL]' in line or 'LOGGING_COMMAND_FAILURE' in line for line in lines):
        raise ValueError('logging transcript lacks complete clean success')


def logging_pending_route(record, route, inputs):
    """Recover completed business only inside this identity-bound handoff."""
    pending=record.get('pending_route')
    if pending:
        if (pending['route']!=route or pending['source_owner']!=record['source_owner'] or
                pending['cluster_uid']!=record['cluster_uid']):
            raise ValueError('pending logging route ownership changed')
        verify_logging_route(pending['receipt'],inputs)
        return pending['receipt']
    # The first cleanup failure predates the checkpoint. Its source directory
    # is still private and leased, its Kubernetes/hardware identities are checked
    # by logging(), and the earlier direct route binds the same launch inputs.
    # Accept only one complete Kafka transcript from that retained boundary.
    if route!='efk-kafka-buffer' or set(record['routes'])!={'efk-direct'}:
        return None
    reference=record['routes']['efk-direct']
    verify_logging_route(reference,inputs)
    found=[]
    for log in STATE.glob('logging-'+route+'-*.log'):
        if not re.fullmatch('logging-'+route+'-[a-f0-9]{12}[.]log',log.name):continue
        stem=log.with_suffix('')
        receipt={'durable':str(stem),'log':str(log),'sha256':hashlib.sha256(log.read_bytes()).hexdigest(),
                 'inputs':reference['inputs'],'input_reference':reference['log']}
        try:verify_logging_route(receipt,inputs)
        except (ValueError,OSError):continue
        if not any('route=efk-kafka-buffer ' in line for line in log.read_text().splitlines()):
            raise ValueError('retained Kafka transcript route differs')
        found.append(receipt)
    if len(found)>1:raise ValueError('ambiguous completed Kafka route; preserve original attempts')
    return found[0] if found else None


def logging_platform_resume_needed(record, directory):
    """Admit only the bound empty platform left before logging preflight."""
    k=load_es().API(str(directory/'kubectl.kubeconfig'))
    if k.get('namespace','kube-system')['metadata']['uid']!=record['cluster_uid']:
        raise ValueError('logging bootstrap Kubernetes identity changed')
    nodes=k.get('nodes')['items']
    addresses={a['address'] for node in nodes for a in node['status']['addresses'] if a['type']=='InternalIP'}
    if (len(nodes)!=6 or addresses!=set(PREVIOUS_HOSTS) or any(node['status']['nodeInfo']['kubeletVersion']!='v1.33.6' for node in nodes)):
        raise ValueError('logging bootstrap recovery node inventory changed')
    if k.get('storageclass','local-path') and k.get('crd','prometheuses.monitoring.coreos.com'):
        return False
    c=yaml.safe_load((directory/'config.yml').read_text())
    if (record['routes'] or c.get('K8S_VER')!='1.33.6' or c.get('REGISTRY_HOST_IP') not in ('192.168.122.243','192.168.122.2')
            or c.get('logging_install')!='no'):
        raise ValueError('logging bootstrap recovery refuses changed inputs or completed business')
    namespaces={obj['metadata']['name'] for obj in k.get('namespaces')['items']}
    if not namespaces<={'default','kube-system','kube-public','kube-node-lease'}:
        raise ValueError('logging bootstrap recovery refuses foreign namespaces')
    if k.get('pv')['items'] or json.loads(k.run('get','pvc','-A','-o','json'))['items']:
        raise ValueError('logging bootstrap recovery refuses retained storage')
    pods=json.loads(k.run('get','pods','-A','-o','json'))['items']
    if any(pod['metadata'].get('namespace')!='kube-system' or
           pod['metadata'].get('labels',{}).get('k8s-app') not in ('calico-node','calico-kube-controllers') for pod in pods):
        raise ValueError('logging bootstrap recovery refuses unrelated workloads')
    return True


def logging_retained_route(api, record):
    routes=('efk-direct','efk-kafka-buffer','loki-direct')
    completed=set(record['routes'])
    if completed!=set(routes[:len(completed)]):
        raise ValueError('logging continuation refuses an unordered route receipt set')
    namespace=api.get('namespace','logging')
    if namespace and completed:
        if len(completed)==len(routes):
            raise ValueError('completed logging routes retain an unexpected namespace')
        labels=namespace['metadata'].get('labels',{})
        expected={'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'logging',
                  'kubeauto.io/logging-solution':routes[len(completed)].split('-',1)[0]}
        if any(labels.get(key)!=value for key,value in expected.items()):
            raise ValueError('logging continuation refuses a foreign next-route namespace')


def logging_conflict_fixture(api, record):
    """Reach Loki's EFK route refusal after the shared credential preflight."""
    labels={'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'logging',
            'kubeauto.io/logging-solution':'loki'}
    namespace=api.get('namespace','logging')
    if namespace and any(namespace['metadata'].get('labels',{}).get(key)!=value for key,value in labels.items()):
        raise ValueError('logging conflict fixture refuses a foreign namespace')
    saved=record.get('loki_conflict_fixture')
    if saved and (not namespace or namespace['metadata']['uid']!=saved['namespace_uid']):
        raise ValueError('logging conflict fixture refuses a changed namespace identity')
    if not namespace:
        api.apply([{'apiVersion':'v1','kind':'Namespace','metadata':{'name':'logging','labels':labels}}])
        namespace=api.get('namespace','logging')
    name='logging-snapshot-s3'
    secret=api.get('secret',name,'logging')
    keys={'s3.client.default.access_key','s3.client.default.secret_key'}
    if secret:
        if (not saved or saved!={'namespace_uid':namespace['metadata']['uid'],'secret_uid':secret['metadata']['uid']} or
                secret['metadata'].get('labels',{}).get('kubeauto.io/es-conflict-fixture')!=record['source_owner'] or
                not keys<=secret.get('data',{}).keys()):
            raise ValueError('logging conflict fixture refuses an unbound Secret')
    else:
        if saved:raise ValueError('logging conflict fixture lost its bound Secret')
        api.apply([{'apiVersion':'v1','kind':'Secret','type':'Opaque','metadata':{
            'name':name,'namespace':'logging','labels':dict(labels,**{'kubeauto.io/es-conflict-fixture':record['source_owner']})},
            'stringData':{key:secrets.token_urlsafe(32) for key in keys}}])
        secret=api.get('secret',name,'logging')
    record['loki_conflict_fixture']={'namespace_uid':namespace['metadata']['uid'],'secret_uid':secret['metadata']['uid']}
    atomic(STATE/'logging-ownership.json',record)


def logging():
    """Run all established logging routes on their own clean source hosts."""
    path=STATE/'logging-ownership.json'
    directory=BASE/'clusters/logging-gate'
    if not path.exists():
        baseline()
        if directory.exists():raise ValueError('logging handoff refuses an existing unrelated cluster directory')
        owner=json.loads((STATE/'ownership.json').read_text())
        record={'source_owner':owner['id'],'hardware':owner['hardware'],'hosts':list(PREVIOUS_HOSTS),'routes':{},'phase':'source-cleaning'}
        atomic(path,record)
        cleanup()
        record['phase']='source-clean';atomic(path,record)
    record=json.loads(path.read_text())
    if record.get('phase')!='source-clean' or (STATE/'ownership.json').exists():
        raise ValueError('logging handoff requires complete native source cleanup')
    for address in PREVIOUS_HOSTS:
        if host_capacity(address)['product_uuid']!=record['hardware'][address]['product_uuid']:
            raise ValueError('logging handoff hardware identity changed')
    resume_platform=False
    if record.get('cluster_uid'):
        current=load_es().API(str(directory/'kubectl.kubeconfig')).get('namespace','kube-system')['metadata']['uid']
        if current!=record['cluster_uid']:raise ValueError('logging retained Kubernetes identity changed')
        resume_platform=logging_platform_resume_needed(record,directory)
        logging_retained_route(load_es().API(str(directory/'kubectl.kubeconfig')),record)
    elif directory.exists():raise ValueError('unbound partial logging platform; preserve it for scoped diagnosis')
    for group in ('platform-default','local-path-provisioner','logging-efk','logging-loki','prometheus','ingress-nginx','kafka'):stage_images(group)
    # These support images are also consumed directly by the established gate.
    stage_images('snapshot-fixtures')
    environment=dict(os.environ,LOGGING_CLUSTER='logging-gate',KUBECONFIG=str(directory/'kubectl.kubeconfig'),
                     ELASTICSEARCH_IMAGE_RECEIPT=str(BASE/'tests/evidence/elasticsearch-images-20261103.json'),
                     LOGGING_REGISTRY_HOST_IP='192.168.122.2',LOGGING_PLATFORM_RESUME='yes' if resume_platform else 'no')
    environment.pop('LOGGING_FOCUS_CASE',None)
    inputs=logging_inputs()
    for solution,delivery in (('efk','direct'),('efk','kafka-buffer'),('loki','direct')):
        route=solution+'-'+delivery
        if route in record['routes']:
            verify_logging_route(record['routes'][route],inputs)
            print('ELASTICSEARCH_LOGGING_ROUTE_REUSED route='+route,flush=True)
            continue
        receipt=logging_pending_route(record,route,inputs)
        if receipt:
            print('ELASTICSEARCH_LOGGING_ROUTE_BUSINESS_REUSED route='+route+' cleanup=pending',flush=True)
        else:
            if solution=='loki':
                logging_conflict_fixture(load_es().API(str(directory/'kubectl.kubeconfig')),record)
            stem=STATE/('logging-'+route+'-'+secrets.token_hex(6))
            print('ELASTICSEARCH_LOGGING_ROUTE_BEGIN route='+route,flush=True)
            log=Path(str(stem)+'.log')
            rc=stream_command(['bash',BASE/'tests/helpers/run-durable-gate.sh',stem,'ELASTICSEARCH_LOGGING_ROUTE_EXIT',
                'bash',BASE/'tests/helpers/logging-regression.sh'],log,dict(environment,LOGGING_SOLUTION=solution,LOGGING_EFK_DELIVERY=delivery),14400)
            if (directory/'kubectl.kubeconfig').exists():
                try:uid=load_es().API(str(directory/'kubectl.kubeconfig')).get('namespace','kube-system')['metadata']['uid']
                except Exception:
                    if rc:raise ValueError('full logging route failed before its platform identity was available: '+route)
                    raise
                if record.get('cluster_uid') and record['cluster_uid']!=uid:raise ValueError('logging route changed its platform identity')
                record['cluster_uid']=uid;atomic(path,record)
            if rc:raise ValueError('full logging route failed: '+route)
            receipt={'durable':str(stem),'log':str(log),'sha256':hashlib.sha256(log.read_bytes()).hexdigest(),'inputs':inputs}
            verify_logging_route(receipt,inputs)
        environment['LOGGING_PLATFORM_RESUME']='no'
        record['pending_route']={'route':route,'source_owner':record['source_owner'],
                                 'cluster_uid':record['cluster_uid'],'receipt':receipt}
        atomic(path,record)
        execute(['env','KUBECONFIG='+environment['KUBECONFIG'],'bash',BASE/'tests/helpers/logging-cleanup.sh'],capture=False,timeout=1800)
        if delivery=='kafka-buffer':execute(['env','KUBECONFIG='+environment['KUBECONFIG'],'bash',BASE/'tests/helpers/kafka-cleanup.sh'],capture=False,timeout=1800)
        execute(['env','KUBECONFIG='+environment['KUBECONFIG'],'bash',BASE/'tests/helpers/logging-cleanup.sh','--verify'],capture=False,timeout=900)
        record['routes'][route]=receipt;record.pop('pending_route');atomic(path,record)
        print('ELASTICSEARCH_LOGGING_ROUTE_PASS route='+route,flush=True)
    logging_cleanup()
    print('ELASTICSEARCH_CASE_PASS id=ES-55',flush=True)
    print('ELASTICSEARCH_LOGGING_SCOPED_CLEAN_PASS routes=3 all_hosts=clean',flush=True)


def logging_cleanup():
    path=STATE/'logging-ownership.json'
    if not path.exists():return
    record=json.loads(path.read_text());directory=BASE/'clusters/logging-gate'
    if record['hosts']!=list(PREVIOUS_HOSTS):raise ValueError('logging cleanup host lease differs')
    for address in PREVIOUS_HOSTS:
        if host_capacity(address)['product_uuid']!=record['hardware'][address]['product_uuid']:
            raise ValueError('logging cleanup hardware identity changed')
    if directory.exists():
        if not record.get('cluster_uid') or load_es().API(str(directory/'kubectl.kubeconfig')).get('namespace','kube-system')['metadata']['uid']!=record['cluster_uid']:
            raise ValueError('logging cleanup refuses an unbound or changed Kubernetes identity')
        product('destroy','logging-gate',timeout=3600)
    execute(['bash',BASE/'tests/helpers/lab-wipe-nodes.sh','--rocky-only',*PREVIOUS_HOSTS],capture=False,timeout=1800)
    execute(['bash',BASE/'tests/helpers/lab-wipe-nodes.sh','--verify','--rocky-only',*HOSTS],capture=False,timeout=1800)
    if directory.exists():raise ValueError('logging cluster directory remains after native cleanup')
    (BASE/'clusters/logging-gate.hosts').unlink(missing_ok=True)
    archive_state(record['source_owner']+'-logging')
    archive=STATE.parent/'kubeauto-elasticsearch-history'/(record['source_owner']+'-logging')
    path.rename(archive/'logging-ownership.json')
    print('ELASTICSEARCH_LOGGING_FINAL_CLEAN_PASS',flush=True)


def prometheus_read(pod, endpoint, **parameters):
    path='/api/v1/namespaces/monitor/pods/http:'+pod+':9090/proxy/api/v1/'+endpoint
    if parameters:path+='?'+urlencode(parameters)
    result=json.loads(api().run('get','--raw',path,timeout=45))
    if result.get('status')!='success':raise ValueError('Prometheus API did not return success')
    return result['data']


def wait_prometheus(pods, description, predicate, timeout=600):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        results=[predicate(pod) for pod in pods]
        if all(results):return
        print('ELASTICSEARCH_WAIT_HEARTBEAT observability='+description+' replicas_ready='+str(sum(results))+'/'+str(len(pods)),flush=True)
        time.sleep(10)
    raise ValueError('Prometheus replicas did not converge: '+description)


def restore_observability():
    path=STATE/'fault-observability.json';record=json.loads(path.read_text())
    c,k=selected_config('standard'),api();ns=c['elasticsearch_namespace']
    for kind,name,namespace,uid in (('deployment',c['elasticsearch_name']+'-metrics',ns,record['deployment_uid']),
            ('servicemonitor',ns+'-'+c['elasticsearch_name']+'-metrics',c['elasticsearch_monitoring_namespace'],record['monitor_uid'])):
        obj=k.get(kind,name,namespace)
        if not obj or obj['metadata']['uid']!=uid:raise ValueError('observability fault recovery refuses replaced resource')
    with load_es().connection(c,k) as request:
        indices=request('GET','/'+record['index']+'/_settings?ignore_unavailable=true')
        if indices:
            actual=indices[record['index']]['settings']['index']['uuid']
            if not record.get('index_uuid') or actual!=record['index_uuid']:raise ValueError('observability cleanup refuses unbound alert index')
            request('DELETE','/'+record['index'])
        if request('GET','/'+record['index']+'/_settings?ignore_unavailable=true'):raise ValueError('alert fixture index remains')
    k.run('-n',c['elasticsearch_monitoring_namespace'],'patch','servicemonitor',ns+'-'+c['elasticsearch_name']+'-metrics',
          '--type=json','-p',json.dumps([{'op':'replace','path':'/spec/selector','value':record['selector']}]))
    k.run('-n',ns,'scale','deployment/'+c['elasticsearch_name']+'-metrics','--replicas=1')
    k.run('-n',ns,'rollout','status','deployment/'+c['elasticsearch_name']+'-metrics','--timeout=5m',timeout=330)
    now=k.get('servicemonitor',ns+'-'+c['elasticsearch_name']+'-metrics',c['elasticsearch_monitoring_namespace'])
    if now['spec']['selector']!=record['selector']:raise ValueError('ServiceMonitor selector recovery incomplete')
    path.unlink()


def prometheus_es_ready(pod, namespace, service):
    targets=[t for t in prometheus_read(pod,'targets')['activeTargets']
             if t.get('labels',{}).get('namespace')==namespace and t['labels'].get('service')==service]
    selector='namespace="'+namespace+'",service="'+service+'",color="green"'
    values=prometheus_read(pod,'query',query='elasticsearch_cluster_health_status{'+selector+'}')['result']
    return len(targets)==1 and targets[0]['health']=='up' and len(values)==1 and float(values[0]['value'][1])==1


def observability():
    baseline()
    # Use the existing production Prometheus and local-path entry points.
    # The monitoring fixture is ephemeral; business PVCs retain three CSI
    # replicas. This avoids making 90 GiB of monitoring claims consume them.
    configuration(local_path_provisioner_install='yes',prom_install='yes',prom_storage_class='local-path')
    product('download','-E','local-path-provisioner',timeout=5400)
    product('download','-E','prometheus',timeout=5400)
    # Reuse the qualified CSI baseline. kubecli passes config.yml as runner
    # extra vars, so a CLI -e override cannot scope this fixture reliably.
    storage_enabled=yaml.safe_load((DIRECTORY/'config.yml').read_text())['longhorn_install']
    configuration(longhorn_install='no')
    try:
        product('setup',NAME,'07',timeout=5400)
    finally:
        configuration(longhorn_install=storage_enabled)
    k=api()
    # local-path runs in the existing kube-system namespace, which must never
    # enter the workload namespace deletion ledger.
    bind_namespace('monitor')
    prometheuses=k.get('prometheus',namespace='monitor')['items']
    if len(prometheuses)!=1 or prometheuses[0]['spec']['replicas']!=2:raise ValueError('observability requires the product two-replica Prometheus')
    k.run('-n','monitor','wait','pod','-l','app.kubernetes.io/name=prometheus','--for=condition=Ready','--timeout=10m',timeout=660)
    pods=sorted(p['metadata']['name'] for p in k.get('pods',namespace='monitor')['items']
                if p['metadata'].get('labels',{}).get('app.kubernetes.io/name')=='prometheus')
    if len(pods)!=2:raise ValueError('observability lacks both actual Prometheus replicas')
    c=selected_config('standard');ns=c['elasticsearch_namespace'];name=c['elasticsearch_name'];secret_name='es-gate-metrics'
    previous_exporter_secret=c['elasticsearch_exporter_secret']
    if k.get('secret',secret_name,ns):raise ValueError('exporter identity fixture already exists')
    k.apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':secret_name,'namespace':ns,'labels':LABELS},'type':'Opaque',
              'stringData':{'username':secret_name,'password':secrets.token_urlsafe(32)}}])
    secret_uid=k.get('secret',secret_name,ns)['metadata']['uid']
    try:
        c=selected_config('standard',elasticsearch_exporter_enabled=True,elasticsearch_exporter_secret=secret_name)
        product('setup',NAME,'07','--tags','elasticsearch',timeout=5400)
        selector='namespace="'+ns+'",service="'+name+'-metrics"'
        def healthy(pod):
            return prometheus_es_ready(pod,ns,name+'-metrics')
        wait_prometheus(pods,'actual-target-and-collector',healthy)
        with load_es().connection(dict(c,elasticsearch_admin_secret=secret_name),k) as request:
            identity=request('GET','/_security/_authenticate')
            if identity.get('roles')!=['kubeauto-metrics']:raise ValueError('exporter did not use the dedicated minimal monitoring role')
            request('GET','/_cluster/health')
            try:request('GET','/'+data_ledger('standard')['index']+'/_search')
            except ValueError as error:
                if 'status=403' not in str(error) or 'security_exception' not in str(error):raise
            else:raise ValueError('monitoring identity can read business documents')
        required={'ElasticsearchClusterRed','ElasticsearchUnassignedShards','ElasticsearchMetricsUnavailable'}
        for pod in pods:
            rules=[r for group in prometheus_read(pod,'rules')['groups'] for r in group['rules'] if r.get('name') in required]
            if {r['name'] for r in rules}!=required or any(r['health']!='ok' or r.get('lastError') for r in rules):
                raise ValueError('Prometheus did not load every product alert rule')
        print('ELASTICSEARCH_CASE_PASS id=ES-46',flush=True)
        monitor=k.get('servicemonitor',ns+'-'+name+'-metrics',c['elasticsearch_monitoring_namespace'])
        deployment=k.get('deployment',name+'-metrics',ns)
        record={'deployment_uid':deployment['metadata']['uid'],'monitor_uid':monitor['metadata']['uid'],'selector':monitor['spec']['selector'],
                'index':'es-gate-alert-'+secrets.token_hex(8)}
        path=STATE/'fault-observability.json'
        if path.exists():raise ValueError('observability has an unrecovered fault')
        with load_es().connection(c,k) as request:
            if request('GET','/'+record['index']+'/_settings?ignore_unavailable=true'):raise ValueError('alert index already exists')
            atomic(path,record)
            request('PUT','/'+record['index'],{'settings':{'number_of_shards':1,'number_of_replicas':1,
                    'index.routing.allocation.require._name':'es-gate-no-such-node'}})
            record['index_uuid']=request('GET','/'+record['index']+'/_settings')[record['index']]['settings']['index']['uuid'];atomic(path,record)
        def alert(pod,names,firing):
            active={a['labels']['alertname'] for a in prometheus_read(pod,'alerts')['alerts']
                    if a.get('state')=='firing' and a.get('labels',{}).get('namespace')==ns and a['labels'].get('service')==name+'-metrics'}
            return names<=active if firing else not names&active
        wait_prometheus(pods,'real-red-and-unassigned-alerts',lambda pod:alert(pod,required-{'ElasticsearchMetricsUnavailable'},True),timeout=540)
        restore_observability()
        wait_prometheus(pods,'data-alerts-recovered',lambda pod:alert(pod,required,False))
        # Remove actual discovery samples, rather than feeding a synthetic 0.
        atomic(path,record)
        changed=dict(record['selector']);changed['matchLabels']=dict(changed.get('matchLabels',{}),**{'kubeauto.io/missing-sample':'true'})
        k.run('-n',c['elasticsearch_monitoring_namespace'],'patch','servicemonitor',ns+'-'+name+'-metrics','--type=json',
              '-p',json.dumps([{'op':'replace','path':'/spec/selector','value':changed}]))
        wait_prometheus(pods,'missing-sample-alert',lambda pod:not prometheus_read(pod,'query',query='up{'+selector+'}')['result']
                        and alert(pod,{'ElasticsearchMetricsUnavailable'},True))
        restore_observability()
        wait_prometheus(pods,'all-alerts-recovered',lambda pod:healthy(pod) and alert(pod,required,False))
        verify_topology('standard');data_ledger('standard')
        print('ELASTICSEARCH_CASE_PASS id=ES-47',flush=True)
    finally:
        primary=sys.exc_info()[1]
        try:
            if (STATE/'fault-observability.json').exists():restore_observability()
            selected_config('standard',elasticsearch_exporter_enabled=False,elasticsearch_exporter_secret=previous_exporter_secret)
            product('setup',NAME,'07','--tags','elasticsearch',timeout=5400)
            with load_es().connection(c,k) as request:request('DELETE','/_security/user/'+secret_name)
            obj=k.get('secret',secret_name,ns)
            if not obj or obj['metadata']['uid']!=secret_uid:raise ValueError('exporter credential fixture was replaced')
            k.run('-n',ns,'delete','secret',secret_name,'--wait=true')
        except Exception as error:
            if primary is None:raise
            print('ELASTICSEARCH_OBSERVABILITY_RESTORE_FAIL reason='+type(error).__name__,file=sys.stderr)
    print('ELASTICSEARCH_OBSERVABILITY_SCOPED_CLEAN_PASS replicas=2 rules=healthy faults=removed',flush=True)


def soak():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    baseline()
    fault_lock = threading.Lock()
    def topology_run(topology):
        injected = False
        def heartbeat(elapsed):
            nonlocal injected
            if elapsed >= 600 and not injected:
                with fault_lock:
                    primary_pod_fault(topology)
                injected = True
        client_job(topology, 'soak', heartbeat=heartbeat)
        if not injected: raise ValueError('soak lacks its declared primary fault')
        verify_topology(topology); data_ledger(topology)
    errors = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(topology_run, topology) for topology in ('standard', 'compact')]
        for task in as_completed(tasks):
            try: task.result()
            except Exception as error: errors.append(error)
    if errors: raise errors[0]
    print('ELASTICSEARCH_SOAK_SCOPED_CLEAN_PASS topologies=2 duration_each_minimum=1800 faults=2', flush=True)


def rally():
    baseline()
    product('download', '-E', 'elasticsearch-qualification', timeout=5400)
    for topology in ('standard', 'compact'):
        c, k = selected_config(topology), api()
        ns, name = c['elasticsearch_namespace'], 'es-gate-rally'
        with load_es().connection(c, k) as request:
            if request('GET', '/kubeauto-rally/_settings?ignore_unavailable=true'):
                raise ValueError('Rally refuses an existing index that its fixed track would delete')
        if k.get('secret', name, ns): raise ValueError('Rally fixture identity is already occupied')
        admin = k.get('secret', c['elasticsearch_admin_secret'] or c['elasticsearch_name']+'-es-elastic-user', ns)
        data = ({'username': admin['data']['username'], 'password': admin['data']['password']} if c['elasticsearch_admin_secret'] else
                {'username': base64.b64encode(b'elastic').decode(), 'password': admin['data']['elastic']})
        secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': ns, 'labels': LABELS}, 'type': 'Opaque', 'data': data}
        try:
            k.apply([secret])
            selected_config(topology, elasticsearch_admin_secret=name)
            client_job(topology, 'rally')
            with load_es().connection(c, k) as request:
                healthy = request('GET', '/_cluster/health/kubeauto-rally?wait_for_status=green&timeout=120s')
                if healthy.get('timed_out') or healthy['status'] != 'green': raise ValueError('Rally index replicas did not recover')
                if request('GET', '/kubeauto-rally/_count')['count'] != 10000: raise ValueError('Rally corpus count differs')
                rows = request('POST', '/kubeauto-rally/_search', {'size': 10000, 'sort': ['sequence'], 'query': {'match_all': {}}})['hits']['hits']
                actual = [r['_source'] for r in rows]
                digest = hashlib.sha256(json.dumps(actual, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
                if digest != '572c88638ae1d75a097fa43f0e8c2f45120960482d642016cccf870f9edc93cf':
                    raise ValueError('Rally corpus canonical content differs')
                print('ELASTICSEARCH_RALLY_CORPUS_VERIFIED topology=' + topology + ' documents=10000 sha256=' + digest, flush=True)
        finally:
            primary = sys.exc_info()[1]
            try:
                selected_config(topology, elasticsearch_admin_secret=c['elasticsearch_admin_secret'])
                with load_es().connection(c, k) as request:
                    # The exact index was proved absent before this job. The
                    # fixed track owns this name for the lifetime of the race.
                    present = request('GET', '/kubeauto-rally/_settings?ignore_unavailable=true')
                    if present: request('DELETE', '/kubeauto-rally')
                    if request('GET', '/kubeauto-rally/_settings?ignore_unavailable=true'): raise ValueError('Rally index remains')
                obj = k.get('secret', name, ns)
                if obj:
                    if not all(obj['metadata'].get('labels', {}).get(key) == value for key, value in LABELS.items()): raise ValueError('foreign Rally identity during cleanup')
                    k.run('-n', ns, 'delete', 'secret', name, '--wait=true')
                data_ledger(topology)
            except Exception as error:
                if primary is None: raise
                print('ELASTICSEARCH_RALLY_CLEANUP_FAIL reason=' + type(error).__name__, file=sys.stderr)
        print('ELASTICSEARCH_RALLY_SCOPED_CLEAN_PASS topology=' + topology, flush=True)
    # P95 is from the separately declared client workload, not interpolated
    # from Rally's P90/P99. The performance stage also requires soak evidence.


def performance():
    rally()
    soak()
    print('ELASTICSEARCH_CASE_PASS id=ES-48', flush=True)
    print('ELASTICSEARCH_PERFORMANCE_SCOPED_CLEAN_PASS', flush=True)

def diagnostics(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    k = api()
    path=STATE/'logging-ownership.json'
    if path.exists() and json.loads(path.read_text()).get('cluster_uid'):
        record=json.loads(path.read_text())
        candidate=load_es().API(str(BASE/'clusters/logging-gate/kubectl.kubeconfig'))
        if candidate.get('namespace','kube-system')['metadata']['uid']!=record['cluster_uid']:
            raise ValueError('logging diagnostics refuses changed cluster identity')
        k=candidate
    for resource in ('nodes', 'pods', 'events', 'elasticsearch', 'kibana', 'pvc', 'pv'):
        try:
            raw = k.run('get', resource, '-A', '-o', 'json', timeout=25)
            path = directory / (resource + '.json')
            path.write_text(raw)
            path.chmod(0o600)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass
    print('ELASTICSEARCH_DIAGNOSTICS_PRIVATE directory=' + str(directory), flush=True)


def cleanup():
    owner_path = STATE / 'ownership.json'
    if not owner_path.exists():
        if DIRECTORY.exists() or (STATE / 'baseline.json').exists() or (STATE/'s3.json').exists() or list(STATE.glob('sysctl-*.json')) or list(STATE.glob('filesystem-*.json')):
            raise ValueError('source cleanup refuses unowned retained state')
        logging_cleanup()
        retained = list(STATE.iterdir())
        if retained:
            if any(not re.fullmatch(r'first-failure-[0-9]+', path.name) or not path.is_dir() or path.is_symlink() for path in retained):
                raise ValueError('source cleanup refuses unowned retained state')
            archive_state('diagnostics-' + uuid.uuid4().hex)
        return
    owner = json.loads(owner_path.read_text())
    if owner['hosts'] != list(HOSTS):
        raise ValueError('source cleanup host lease differs')
    if (STATE / 'baseline.json').exists():
        baseline()
        cleanup_namespaces()
        restore_faults()
        cleanup_csi()
        if (STATE/'dr/ownership.json').exists(): dr_fixture('cleanup')
        k = api()
        for namespace in k.get('namespaces')['items']:
            name = namespace['metadata']['name']
            if name in ('es-standard', 'es-compact'):
                if not all(namespace['metadata'].get('labels', {}).get(a) == b for a, b in LABELS.items()):
                    raise ValueError('foreign scenario namespace during cleanup')
                selected_config(name.removeprefix('es-'), elasticsearch_state='absent', elasticsearch_requests=[])
                product('setup', NAME, '07', '--tags', 'elasticsearch')
                k.run('delete', 'namespace', name, '--wait=true', '--timeout=10m', timeout=660)
        cleanup_workload_namespaces()
        deadline = time.monotonic() + 600
        while k.get('pv')['items'] and time.monotonic() < deadline:
            print('ELASTICSEARCH_WAIT_HEARTBEAT cleanup=CSI-volumes', flush=True)
            time.sleep(10)
        if k.get('pv')['items']:
            raise ValueError('source cleanup refuses retained PVs')
        if k.get('namespace', 'longhorn-system'):
            configuration(longhorn_action='uninstall', longhorn_uninstall_id='es-fixture-' + owner['id'], longhorn_requests=[])
            product('setup', NAME, '09', timeout=5400)
        for address in HOSTS:
            filesystem(address, 'remove')
        sysctl_fixture('restore')
        product('destroy', NAME, timeout=3600)
    elif DIRECTORY.exists():
        # A failed bootstrap can only own the empty admitted hosts, never a
        # Longhorn mount. Preserve any later filesystem rather than guessing.
        if list(STATE.glob('filesystem-*.json')):
            raise ValueError('partial source retains storage requiring native recovery')
        product('destroy', NAME, timeout=3600)
    execute(['bash', BASE / 'tests/helpers/lab-wipe-nodes.sh', '--rocky-only', *HOSTS], capture=False, timeout=1800)
    execute(['bash', BASE / 'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', *HOSTS], capture=False, timeout=1800)
    if DIRECTORY.exists() or list(STATE.glob('filesystem-*.json')) or list(STATE.glob('sysctl-*.json')):
        raise ValueError('source cleanup postconditions are incomplete')
    (STATE / 'baseline.json').unlink(missing_ok=True)
    (STATE / 'namespaces.json').unlink(missing_ok=True)
    s3_fixture('cleanup')
    archive_state(owner['id'])
    print('ELASTICSEARCH_SOURCE_CLEAN_PASS', flush=True)


def cleanup_gate():
    cleanup()
    for name in ('elasticsearch-gate','elasticsearch-dr','logging-gate'):
        if (BASE/'clusters'/name).exists():raise ValueError('final cleanup retains a leased cluster context')
    if any(STATE.iterdir()):raise ValueError('final cleanup retains unarchived owned state')
    targets=HOSTS+('192.168.122.135','192.168.122.40','192.168.122.72')
    execute(['bash',BASE/'tests/helpers/lab-wipe-nodes.sh','--verify','--rocky-only',*targets],capture=False,timeout=1800)
    print('ELASTICSEARCH_CASE_PASS id=ES-57',flush=True)
    print('LAB_CLEAN_VERIFY_PASS',flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('module', choices=('admission', 'registry-recover', 'bootstrap', 'bootstrap-resume', 'storage', 'csi-probe', 'standard', 'compact', 'diagnostics', 'cleanup', 'business', 'lifecycle', 'security', 'security-identity', 'security-certificates', 'security-network', 'security-s3', 'security-certificate-restore', 'faults', 'restore-faults', 'maintenance', 'backup', 'backup-source', 'backup-refusal', 'backup-restore', 'backup-features', 'dr-bootstrap', 'dr', 'upgrade', 'upgrade-platform', 'coexistence', 'coexistence-isolation', 'coexistence-restores', 'logging', 'logging-cleanup', 'cleanup-probes', 'observability', 'soak', 'rally', 'performance'))
    args = p.parse_args()
    if os.environ.get('ELASTICSEARCH_PARENT_LEASE_HELD') != 'yes':
        raise ValueError('fixed parent pool lease is required')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ['PATH'] = str(BASE / 'kube-bin') + ':' + str(BASE / 'extra-bin') + ':' + os.environ['PATH']
    if args.module == 'restore-faults':
        baseline(); restore_faults()
    elif args.module in ('standard', 'compact'):
        install(args.module)
    elif args.module == 'diagnostics':
        diagnostics(STATE.parent / 'kubeauto-elasticsearch-history' / ('first-failure-' + uuid.uuid4().hex))
    elif args.module == 'cleanup':
        cleanup_gate()
    else:
        globals()[args.module.replace('-', '_')]()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print('ELASTICSEARCH_LAB_REFUSED class=unclassified reason=' + str(exc), file=sys.stderr)
        raise SystemExit(1)
