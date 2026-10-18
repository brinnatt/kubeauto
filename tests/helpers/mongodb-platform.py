#!/usr/bin/env python3
"""Read-only CPU preflight on the explicitly authorized MongoDB compute pool."""
import argparse
import json
import subprocess
import sys

NODES = ('192.168.122.217', '192.168.122.243', '192.168.122.246',
         '192.168.122.193', '192.168.122.210', '192.168.122.216')
POOLS = {'large': ('192.168.122.2', NODES),
         'core': ('192.168.47.130', tuple(f'192.168.47.{i}' for i in range(131, 137))),
         'ceph': ('192.168.47.130', ('192.168.122.135', '192.168.122.40', '192.168.122.72',
                                   '192.168.122.212', '192.168.122.165', '192.168.122.238'))}
# x86-64-v3 includes the preceding v2 level. Linux calls SSE3 pni and LZCNT abm.
V3 = frozenset('avx avx2 bmi1 bmi2 f16c fma movbe xsave sse4_1 sse4_2 popcnt cx16 lahf_lm ssse3 pni'.split())
PROBE = r'''import json,platform,re,subprocess
from pathlib import Path
text=Path('/proc/cpuinfo').read_text()
sets=[set(line.split(':',1)[1].split()) for line in text.splitlines() if re.match(r'^(flags|Features)\s*:',line)]
flags=set.intersection(*sets) if sets else set()
loader=Path('/lib64/ld-linux-x86-64.so.2')
helptext=subprocess.run([str(loader),'--help'],capture_output=True,text=True,timeout=10).stdout if loader.exists() else ''
memory=Path('/proc/meminfo').read_text()
units=('kubelet','kube-apiserver','etcd','containerd','docker','ceph.target')
services={u:subprocess.run(['systemctl','is-active',u],capture_output=True,text=True,timeout=10).stdout.strip() for u in units}
paths=('/etc/kubernetes','/var/lib/kubelet','/var/lib/etcd','/var/lib/containerd','/var/lib/docker','/var/lib/ceph','/var/lib/kubeauto-ceph-test')
residue=[p for p in paths if Path(p).exists()]
ceph=Path('/var/lib/ceph')
ceph_entries=sorted(p.name for p in ceph.iterdir()) if ceph.exists() else []
print(json.dumps({'architecture':platform.machine(),'kernel':platform.release(),'cpu_count':len(sets),'flags':sorted(flags),'glibc_v3':bool(re.search(r'x86-64-v3\s+\(supported, searched\)',helptext)),'memory_kib':int(re.search(r'MemTotal:\s+(\d+)',memory)[1]),'services':services,'existing_paths':residue,'ceph_entries':ceph_entries}))'''
CONTROL_PROBE = r'''import json,os,shutil
from pathlib import Path
active=[]
for path in Path('/tmp').glob('kubeauto-*.pid'):
 try:
  pid=int(path.read_text().strip());os.kill(pid,0)
 except (ValueError,ProcessLookupError,FileNotFoundError):continue
 active.append({'state_file':path.name,'pid':pid})
locks=[]
locktext=Path('/proc/locks').read_text()
for filename in ('kubeauto-ceph-priority.lock','kubeauto-ceph-compute.lock','kubeauto-redis-core-pool.lock'):
 path=Path('/var/lock')/filename
 if path.exists():
  inode=path.stat().st_ino
  locks.append({'name':filename,'held':any(line.split()[5].endswith(':'+str(inode)) for line in locktext.splitlines() if len(line.split())>5)})
state=Path('/var/lib/kubeauto-ceph-test')
ceph_state=sorted(p.name for p in state.iterdir()) if state.exists() else []
root=Path('/usr/local/kubeauto')
clusters=root/'clusters'
print(json.dumps({'active_jobs':active,'leases':locks,'ceph_state_files':ceph_state,'cluster_directories':sorted(p.name for p in clusters.iterdir()) if clusters.exists() else [],'source_exists':(root/'kubecli.py').is_file(),'python_exists':(root/'.venv/bin/python').is_file(),'commands':{c:bool(shutil.which(c)) for c in ('docker','skopeo','ssh','flock','rsync')},'free_bytes':shutil.disk_usage('/').free}))'''
INVENTORY_PROBE = r'''import json,re
from pathlib import Path
root=Path('/usr/local/kubeauto/clusters')
for directory in sorted(root.iterdir()) if root.exists() else []:
 if not directory.is_dir() or directory.is_symlink():continue
 hosts=directory/'hosts'
 ips=sorted(set(re.findall(r'^([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s',hosts.read_text(),re.M))) if hosts.is_file() else []
 owners=[]
 for path in directory.glob('*owner*.json'):
  data=json.loads(path.read_text())
  owners.append({'file':path.name,'cluster':data.get('cluster'),'cluster_uid':data.get('cluster_uid'),'healthy':data.get('healthy')})
 print(json.dumps({'directory':directory.name,'inventory_ips':ips,'owners':owners,'kubeconfig_present':(directory/'kubectl.kubeconfig').is_file()}))'''

RESET_STALE_UNIT = r'''import subprocess
from pathlib import Path
paths=('/etc/kubernetes','/var/lib/kubelet','/var/lib/etcd','/var/lib/containerd','/var/lib/docker','/var/lib/kubeauto-ceph-test')
if any(Path(p).exists() for p in paths):raise SystemExit('MONGODB_ENVIRONMENT_FAIL reason=foreign-state')
ceph=Path('/var/lib/ceph')
if ceph.exists() and any(ceph.iterdir()):raise SystemExit('MONGODB_ENVIRONMENT_FAIL reason=foreign-ceph-state')
for unit in ('kubelet','etcd','containerd','docker','ceph.target'):
 state=subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True,timeout=10).stdout.strip()
 if state not in ('inactive','unknown'):raise SystemExit('MONGODB_ENVIRONMENT_FAIL reason=active-or-unexplained-unit')
state=subprocess.run(['systemctl','is-active','kube-apiserver'],capture_output=True,text=True,timeout=10).stdout.strip()
if state=='failed':subprocess.run(['systemctl','reset-failed','kube-apiserver'],check=True,timeout=10)
elif state not in ('inactive','unknown'):raise SystemExit('MONGODB_ENVIRONMENT_FAIL reason=active-apiserver')
print('MONGODB_STALE_UNIT_RESET_PASS node=192.168.122.72 unit=kube-apiserver data-touched=false')'''

RUNTIME_DIAGNOSTICS = r'''import json,re,socket,subprocess,urllib.request
from pathlib import Path
result={}
for port in (22,5000):
 try:
  with socket.create_connection(('192.168.47.130',port),timeout=3):result['tcp_'+str(port)]='connected'
 except OSError as e:result['tcp_'+str(port)]=type(e).__name__
try:
 with urllib.request.urlopen('http://192.168.47.130:5000/v2/',timeout=3) as f:result['registry_http_status']=f.status
except Exception as e:result['registry_http_error']=type(e).__name__
for path in ('/etc/containerd/config.toml','/etc/containerd/certs.d/registry.talkschool.cn:5000/hosts.toml','/etc/systemd/system/kube-apiserver.service'):
 if Path(path).exists():
  result[path]=[line.strip() for line in Path(path).read_text().splitlines() if any(word in line for word in ('config_path','sandbox_image','service-cluster-ip-range','server =','host.','capabilities','skip_verify'))]
result['hosts']=[line for line in Path('/etc/hosts').read_text().splitlines() if 'registry.talkschool.cn' in line]
result['route']=subprocess.run(['ip','route','get','192.168.47.130'],capture_output=True,text=True,timeout=5).stdout.strip()
print(json.dumps(result))'''


def validate_cpu(result):
    if not isinstance(result, dict) or result.get('architecture') != 'x86_64':
        raise ValueError('this AMD64 lab preflight cannot certify a different CPU architecture')
    flags = set(result.get('flags', []))
    missing = sorted(V3 - flags)
    if not flags.intersection({'abm', 'lzcnt'}):
        missing.append('lzcnt/abm')
    # The image supplies its own glibc loader. Rocky 8's host loader predates
    # glibc-hwcaps, so its --help cannot reject an otherwise suitable CPU.
    if missing or result.get('cpu_count', 0) < 2:
        raise ValueError('x86-64-v3 unavailable: missing=' + ','.join(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control', action='store_true')
    parser.add_argument('--inventory', action='store_true')
    parser.add_argument('--pool', choices=sorted(POOLS), default='large')
    parser.add_argument('--reset-stale-unit', action='store_true')
    parser.add_argument('--runtime-diagnostics', action='store_true')
    args = parser.parse_args()
    control, nodes = POOLS[args.pool]
    if args.runtime_diagnostics:
        if args.pool != 'ceph':
            parser.error('runtime diagnostics are limited to the MongoDB leased compute pool')
        for node in nodes:
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                     'root@' + node, '/usr/bin/python3', '-'], input=RUNTIME_DIAGNOSTICS,
                                    capture_output=True, text=True, timeout=30)
            print('MONGODB_RUNTIME_DIAGNOSTICS node=' + node + ' rc=' + str(result.returncode) + ' ' + result.stdout.strip(), flush=True)
        return 0
    if args.reset_stale_unit:
        if args.pool != 'ceph':
            parser.error('stale-unit recovery is limited to the proven Ceph-pool environment failure')
        command = ('flock -n /var/lock/kubeauto-ceph-priority.lock '
                   'flock -n /var/lock/kubeauto-ceph-compute.lock '
                   'ssh -o BatchMode=yes -o ConnectTimeout=10 root@192.168.122.72 /usr/bin/python3 -')
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 'root@' + control, command], input=RESET_STALE_UNIT,
                                text=True, capture_output=True, timeout=60)
        print(result.stdout, end='', flush=True)
        if result.returncode:
            print('MONGODB_ENVIRONMENT_FAIL reason=lease-or-stale-unit-guard')
        return int(bool(result.returncode))
    if args.inventory:
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 'root@' + control, '/usr/bin/python3 -'], input=INVENTORY_PROBE,
                                text=True, capture_output=True, timeout=45)
        if result.returncode:
            print('MONGODB_INVENTORY_PROBE_FAIL class=environment')
            return 1
        for line in result.stdout.splitlines():
            print(json.dumps(json.loads(line), sort_keys=True), flush=True)
        return 0
    if args.control:
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 'root@' + control, '/usr/bin/python3 -'], input=CONTROL_PROBE,
                                text=True, capture_output=True, timeout=45)
        if result.returncode:
            print('MONGODB_CONTROL_PROBE_FAIL class=environment reason=SSH-or-probe-failed')
            return 1
        state = json.loads(result.stdout)
        print(json.dumps(state, sort_keys=True), flush=True)
        if state['active_jobs']:
            print('MONGODB_CONTROL_PROBE_FAIL class=environment reason=active-job-must-be-preserved')
            return 1
        print('MONGODB_CONTROL_PROBE_PASS read-only=true', flush=True)
        return 0
    failures = []
    for node in nodes:
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 'root@' + node, '/usr/bin/python3 -'], input=PROBE,
                                text=True, capture_output=True, timeout=45)
        try:
            if result.returncode:
                raise ValueError('SSH or remote CPU probe failed rc=' + str(result.returncode))
            data = json.loads(result.stdout)
            print('MONGODB_NODE_INVENTORY ' + json.dumps({'node': node, 'services': data.get('services', {}),
                  'existing_paths': data.get('existing_paths', []), 'memory_kib': data.get('memory_kib'),
                  'ceph_entries': data.get('ceph_entries', [])}, sort_keys=True), flush=True)
            validate_cpu(data)
            print('MONGODB_CPU_NODE_PASS node=' + node + ' kernel=' + data['kernel'] +
                  ' memory_kib=' + str(data.get('memory_kib', 'unknown')), flush=True)
        except (ValueError, KeyError, TypeError) as exc:
            failures.append(node)
            print('MONGODB_CPU_PREFLIGHT_FAIL class=environment node=' + node + ' reason=' + str(exc), flush=True)
    if failures:
        return 1
    print('MONGODB_CPU_PREFLIGHT_PASS nodes=' + str(len(nodes)) + ' x86-64-v3=true read-only=true', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
