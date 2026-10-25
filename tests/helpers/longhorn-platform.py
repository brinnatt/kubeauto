#!/usr/bin/env python3
"""LONGHORN-05: reversible host refusals through config+kubecli, then real I/O.

Pinned upstream: Longhorn v1.12.1 install/multidisk documentation and
longhorn-manager controller/node_controller.go and csi/deployment.go. The
MountPropagation condition describes Pod configuration; cross-node fsync and
read-back, rather than that condition alone, prove the actual client path.
"""
import argparse
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import subprocess
import uuid

import yaml

NS = 'longhorn-system'
RECORD = 'longhorn-platform.json'
ROOT_REFUSAL = 'Longhorn data path requires a dedicated mount root, not a system directory'


def service_action(request, action, root='/'):
    """Remote, journaled runtime mask. Never change persistent enablement.

    Publish the mask with a hard link to an already recorded symlink inode.
    Thus a lost SSH response cannot leave an unidentifiable mask. Restore only
    that inode, and refuse foreign masks, changed boot identities or units.
    ``root`` exists solely for deterministic local filesystem contracts.
    """
    import hashlib
    import json
    import os
    from pathlib import Path
    import stat
    import subprocess
    import uuid

    root = Path(root)
    run_id = str(uuid.UUID(request['run_id']))
    if action not in ('inspect', 'inject', 'restore'):
        raise ValueError('invalid service action')
    runtime = root/'run/systemd/system'
    journal = root/('run/kubeauto-longhorn-platform-'+run_id+'.json')
    temporary = runtime/('.kubeauto-longhorn-'+run_id)
    mask = runtime/'iscsid.service'
    units = ('iscsid.service', 'iscsid.socket')

    def run(args, check=True):
        result = subprocess.run(args, text=True, capture_output=True, timeout=30)
        if check and result.returncode:
            raise ValueError('platform service command failed: '+args[0])
        return result

    def identity(path):
        info = path.lstat()
        return {'dev': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
                'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode),
                'target': os.readlink(path) if stat.S_ISLNK(info.st_mode) else None}

    def fingerprint(path):
        info = path.stat()
        return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'uid': info.st_uid, 'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode)}

    def machine():
        return {'machine': (root/'etc/machine-id').read_text().strip(),
                'boot': (root/'proc/sys/kernel/random/boot_id').read_text().strip()}

    def sessions():
        result = run(['iscsiadm', '-m', 'session'], check=False)
        if result.returncode != 21 or result.stdout.strip():
            raise ValueError('platform refusal requires no iSCSI sessions')

    def state():
        result = {}
        for unit in units:
            raw = run(['systemctl', 'show', unit, '--property=LoadState,ActiveState,UnitFileState,FragmentPath']).stdout
            row = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
            if (row.get('LoadState') != 'loaded' or row.get('ActiveState') not in ('active', 'inactive') or
                    row.get('UnitFileState') not in ('enabled', 'disabled', 'static', 'indirect') or
                    not row.get('FragmentPath', '').startswith('/')):
                raise ValueError('platform service baseline is masked, transitional or unknown')
            row['fragment'] = fingerprint(root/row['FragmentPath'].lstrip('/'))
            result[unit] = row
        return {'identity': machine(), 'units': result}

    def save(row, exclusive=False):
        target = journal if exclusive else journal.with_name(journal.name+'.'+uuid.uuid4().hex)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(row, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
            if not exclusive:
                target.replace(journal)
        finally:
            if not exclusive and target.exists():
                target.unlink()

    if action == 'inspect':
        if journal.exists() or journal.is_symlink() or mask.exists() or mask.is_symlink() or temporary.exists() or temporary.is_symlink():
            raise ValueError('platform service fixture or foreign runtime mask exists')
        sessions()
        before = state()
        if before['units']['iscsid.service']['ActiveState'] != 'active':
            raise ValueError('platform requires a healthy original iSCSI daemon')
        return before
    if action == 'inject':
        if service_action(request, 'inspect', root) != request['before']:
            raise ValueError('platform service baseline changed before injection')
        row = {'run_id': run_id, 'before': request['before'], 'mask_identity': None}
        save(row, exclusive=True)
        # Stop socket activation first; no session or mounted volume is owned.
        run(['systemctl', 'stop', 'iscsid.socket'])
        run(['systemctl', 'stop', 'iscsid.service'])
        os.symlink('/dev/null', temporary)
        row['mask_identity'] = identity(temporary)
        save(row)
        os.link(temporary, mask, follow_symlinks=False)
        run(['systemctl', 'daemon-reload'])
        actual = run(['systemctl', 'show', 'iscsid.service', '--property=LoadState', '--value']).stdout.strip()
        if actual != 'masked':
            raise ValueError('platform runtime service mask did not take effect')
        return {'mask_identity': row['mask_identity'], 'load_state': actual}
    if not journal.exists() and not journal.is_symlink():
        if mask.exists() or mask.is_symlink() or temporary.exists() or temporary.exists() or temporary.is_symlink():
            raise ValueError('platform refuses unjournaled runtime mask cleanup')
        if state() != request['before']:
            raise ValueError('platform service state changed without its journal')
        return {'restored': True}
    if journal.is_symlink() or journal.stat().st_uid != os.geteuid() or journal.stat().st_mode & 0o077:
        raise ValueError('platform foreign service journal')
    row = json.loads(journal.read_text())
    if row['run_id'] != run_id or row['before'] != request['before'] or machine() != request['before']['identity']:
        raise ValueError('platform service journal or boot identity changed')
    for unit, original in row['before']['units'].items():
        if fingerprint(root/original['FragmentPath'].lstrip('/')) != original['fragment']:
            raise ValueError('platform original unit fragment changed')
    for path in (mask, temporary):
        if path.exists() or path.is_symlink():
            if not row['mask_identity'] or identity(path) != row['mask_identity']:
                raise ValueError('platform refuses to remove a foreign mask inode')
    for path in (mask, temporary):
        if path.is_symlink():
            path.unlink()
    run(['systemctl', 'daemon-reload'])
    for unit in units:
        verb = 'start' if row['before']['units'][unit]['ActiveState'] == 'active' else 'stop'
        run(['systemctl', verb, unit])
    sessions()
    if state() != row['before']:
        raise ValueError('platform service restoration differs from original baseline')
    journal.unlink()
    return {'restored': True}


def remote_service(lab, request, action):
    script = inspect.getsource(service_action) + '\nimport json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(service_action(r["request"],r["action"]),sort_keys=True))\n'
    result = lab.remote(request['node'], ['python3', '-c', script],
                        stdin=json.dumps({'request': request, 'action': action}), timeout=240)
    return json.loads(result.stdout)


def empty(lab):
    for kind in ('volumes.longhorn.io', 'engines.longhorn.io', 'replicas.longhorn.io'):
        if lab.get(kind, namespace=NS)['items']:
            raise ValueError('platform host refusal requires no native volumes or processes')
    if any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io'
           for p in lab.get('pv')['items']):
        raise ValueError('platform host refusal refuses retained Longhorn PVs')
    for name in ('longhorn-request-input.json', 'longhorn-install-business.json',
                 'longhorn-security-tls.json', 'longhorn-encryption-key.json'):
        if (lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink():
            raise ValueError('platform refuses unfinished product input or business fixtures')
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    if config.get('longhorn_requests') or config.get('longhorn_data_engines', ['v1']) != ['v1']:
        raise ValueError('platform requires the owned idle V1 configuration')


def host_observation(lab):
    state = lab.owner()
    script = '''import hashlib,json,os,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin)
args=['/usr/local/sbin/kubeauto-longhorn-host-check']
if r['disk']:
 args+=['--data-path',r['path'],'--cluster-uid',r['cluster_uid'],'--node-uid',r['node_uid']]
result=subprocess.run(args,check=True,text=True,capture_output=True,timeout=60)
row={'check':json.loads(result.stdout),'files':{},'kernel':os.uname().release,
     'iscsi_version':subprocess.run(['iscsiadm','--version'],check=True,text=True,capture_output=True,timeout=30).stdout.strip(),
     'machine_id':Path('/etc/machine-id').read_text().strip(),
     'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
for name in ['/etc/modules-load.d/kubeauto-longhorn.conf','/usr/local/sbin/kubeauto-longhorn-host-check']+(['/etc/kubeauto/longhorn-disk.json','/etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf'] if r['disk'] else []):
 p=Path(name);assert p.is_file() and not p.is_symlink()
 s=p.stat();row['files'][name]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'uid':s.st_uid,'gid':s.st_gid,'mode':s.st_mode&0o777}
if r['disk']:
 receipt=json.loads(Path('/etc/kubeauto/longhorn-disk.json').read_text());assert receipt['uuid']==r['uuid'] and receipt['path']==r['path']
 guard=Path('/etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf').read_text()
 assert 'RequiresMountsFor='+r['path'] in guard and 'ExecStartPre=/usr/bin/mountpoint -q -- '+r['path'] in guard
 row['receipt']=receipt
print(json.dumps(row,sort_keys=True))
'''
    rows = {}
    for node in lab.NODES:
        disk = state['disks'].get(node)
        name = 'longhorn-node-'+node.rsplit('.', 1)[1]
        request = {'disk': bool(disk), 'path': lab.DATA, 'cluster_uid': state['cluster_uid'],
                   'node_uid': state['node_uids'][name], 'uuid': disk['uuid'] if disk else None}
        if disk:
            lab.remote(node, ['python3', '-c', lab.disk_parent_script(), 'check'], stdin=json.dumps(disk))
            lab.remote(node, ['python3', '-c', lab.disk_cleanup_script(), 'check'], stdin=json.dumps(disk))
        rows[node] = json.loads(lab.remote(node, ['python3', '-c', script], stdin=json.dumps(request)).stdout)
    return rows


def propagation(lab):
    """Verify native conditions and actual manager/CSI Pod mount contracts."""
    nodes = {'longhorn-node-'+n.rsplit('.', 1)[1] for n in lab.WORKERS}
    native = lab.get('nodes.longhorn.io', namespace=NS)['items']
    conditions = {n['metadata']['name']: n['status'].get('conditions', []) for n in native}
    for node in nodes:
        if not any(c.get('type') == 'MountPropagation' and c.get('status') == 'True'
                   for c in conditions.get(node, [])):
            raise ValueError('platform manager MountPropagation is not True')
    pods = lab.get('pods', namespace=NS)['items']
    result = {}
    for app, container_name, required in (
            ('longhorn-manager', 'longhorn-manager', {'longhorn': '/var/lib/longhorn/'}),
            ('longhorn-csi-plugin', 'longhorn-csi-plugin',
             {'kubernetes-csi-dir': '/var/lib/kubelet/plugins/kubernetes.io/csi',
              'pods-mount-dir': '/var/lib/kubelet/pods'})):
        selected = [p for p in pods if p['metadata'].get('labels', {}).get('app') == app]
        # The manager is limited to product client nodes; upstream CSI's
        # system-managed DaemonSet uses the empty default node selector.
        expected = nodes if app == 'longhorn-manager' else {
            'longhorn-node-'+address.rsplit('.', 1)[1] for address in lab.NODES}
        if len(selected) != len(expected) or {p['spec'].get('nodeName') for p in selected} != expected:
            raise ValueError('platform propagation Pod topology differs')
        for pod in selected:
            if pod['metadata'].get('deletionTimestamp') or not any(
                    c.get('type') == 'Ready' and c.get('status') == 'True'
                    for c in pod.get('status', {}).get('conditions', [])):
                raise ValueError('platform propagation Pod is not Ready')
            containers = [c for c in pod['spec']['containers'] if c['name'] == container_name]
            if len(containers) != 1 or containers[0].get('securityContext', {}).get('privileged') is not True:
                raise ValueError('platform propagation requires the privileged owning container')
            volumes = {v['name']: v.get('hostPath', {}).get('path') for v in pod['spec'].get('volumes', [])}
            mounts = {m['name']: m for m in containers[0].get('volumeMounts', [])}
            for name, path in required.items():
                if volumes.get(name) != path or mounts.get(name, {}).get('mountPropagation') != 'Bidirectional':
                    raise ValueError('platform hostPath or Bidirectional mount contract differs')
            result[app+'/'+pod['spec']['nodeName']] = pod['metadata']['uid']
    return result


def product_service_refusal(lab, relay_pid, node):
    # Ansible host failures do not carry the controller's PRODUCT_FAIL marker.
    # Match the exact owning task and node; arbitrary setup failure is not PASS.
    identity = lab.process_identity(relay_pid)
    if not identity or identity[0][-2:] != [str(Path(lab.__file__).resolve()).encode(), b'relay-maintain']:
        raise ValueError('platform service refusal requires a live owned relay')
    command = [sys.executable, str(lab.BASE/'kubecli.py'), 'setup', lab.CLUSTER, '09']
    with tempfile.TemporaryFile(mode='w+') as observation:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True,
                                   stdout=observation, stderr=subprocess.STDOUT)
        started = time.monotonic(); heartbeat = started
        with lab.scoped_cleanup(lambda: lab.stop_product(process, command)):
            while process.poll() is None:
                if lab.process_identity(relay_pid) != identity:
                    raise ValueError('platform registry supervisor exited during host refusal')
                now = time.monotonic()
                if now-started >= 7200:
                    raise TimeoutError('platform host-refusal product step exceeded two hours')
                if now-heartbeat >= 15:
                    print('LONGHORN_PLATFORM_HEARTBEAT step=service-refusal elapsed='+str(int(now-started)), flush=True)
                    heartbeat = now
                time.sleep(1)
            observation.seek(0); output = observation.read()
            rc = process.returncode
    lab.write_private(lab.DIRECTORY/'longhorn-platform-service-refusal-output.json',
                      {'rc': rc, 'output': output})
    start = output.find('TASK [longhorn : Enable the iSCSI daemon]')
    segment = output[start:] if start >= 0 else ''
    if (not rc or ('fatal: ['+node+']') not in segment or 'masked' not in segment.lower() or
            lab.process_identity(relay_pid) != identity):
        raise ValueError('platform product did not refuse its unavailable iSCSI daemon at the owning task')
    print('LONGHORN_PLATFORM_DEPENDENCY_REFUSAL_PASS node='+node, flush=True)


def journal(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('symlink platform journal')
    row = json.loads(path.read_text())
    if row['run_id'] != lab.owner()['run_id'] or row['service']['node'] != lab.WORKERS[0]:
        raise ValueError('foreign platform journal')
    return row


def clean(lab, relay_pid, *, failed=False):
    path = lab.DIRECTORY/RECORD
    if not path.exists() and not path.is_symlink():
        return
    row = journal(lab)
    # Restore host service before health or business cleanup can depend on it.
    if row.get('service_intent'):
        remote_service(lab, row['service'], 'restore')
    lab.restore_request_input()
    if not row.get('reconciled'):
        # A rejected root input may have been rendered before validation.
        # Re-render and converge only the restored customer configuration.
        lab.supervise_core(relay_pid, 'longhorn-setup')
        lab.installation_health()
        row['reconciled'] = True; lab.write_private(path, row)
    if row.get('business_intent'):
        lab.cleanup_business(relay_pid=relay_pid)
    if failed:
        empty(lab); lab.installation_health()
        if ((lab.DIRECTORY/'config.yml').read_text() != row['original_config'] or
                host_observation(lab) != row['hosts'] or lab.shared_snapshot_identity() != row['snapshot_identity']):
            raise ValueError('platform failure recovery differs from the original baseline')
        # Keep the failed attempt for diagnosis without blocking the next
        # focused run on an already recovered fixture journal.
        lab.write_private(lab.DIRECTORY/('longhorn-platform-recovered-'+uuid.uuid4().hex+'.json'), row)
        path.unlink()
    print('LONGHORN_PLATFORM_FIXTURES_RESTORED', flush=True)


def business(lab, relay_pid):
    lab.installation_health(); empty(lab)
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink():
        raise ValueError('previous platform module needs scoped recovery')
    config_path = lab.DIRECTORY/'config.yml'
    original = config_path.read_text()
    request = {'run_id': lab.owner()['run_id'], 'node': lab.WORKERS[0]}
    request['before'] = remote_service(lab, request, 'inspect')
    row = {'run_id': request['run_id'], 'service': request, 'original_config': original,
           'hosts': host_observation(lab), 'objects': lab.idempotence_snapshot(NS),
           'snapshot_identity': lab.shared_snapshot_identity(), 'business_intent': False,
           'service_intent': False, 'passed': False}
    lab.write_private(path, row)
    with lab.scoped_cleanup(lambda: clean(lab, relay_pid)):
        invalid = yaml.safe_load(original); invalid['longhorn_data_path'] = '/'
        desired = yaml.safe_dump(invalid, sort_keys=False)
        lab.write_private(lab.DIRECTORY/'longhorn-request-input.json',
                          {'run_id': row['run_id'], 'original': original, 'desired': desired})
        config_path.write_text(desired)
        lab.supervise_core(relay_pid, 'longhorn-setup', expected_refusal=ROOT_REFUSAL)
        lab.restore_request_input()
        if lab.idempotence_snapshot(NS) != row['objects'] or host_observation(lab) != row['hosts']:
            raise ValueError('platform root-path rejection changed protected resources')
        print('LONGHORN_PLATFORM_ROOT_DISK_REFUSAL_PASS', flush=True)
        row['service_intent'] = True; lab.write_private(path, row)
        remote_service(lab, request, 'inject')
        try:
            product_service_refusal(lab, relay_pid, request['node'])
        finally:
            remote_service(lab, request, 'restore')
        if lab.idempotence_snapshot(NS) != row['objects'] or host_observation(lab) != row['hosts']:
            raise ValueError('platform unavailable-service refusal changed protected resources')
        # The accepted positive path always uses the restored customer input.
        lab.supervise_core(relay_pid, 'longhorn-setup')
        lab.installation_health()
        row['reconciled'] = True; lab.write_private(path, row)
        propagation(lab)
        row['business_intent'] = True; lab.write_private(path, row)
        lab.business('install', relay_pid=relay_pid)
        if (config_path.read_text() != original or host_observation(lab) != row['hosts'] or
                lab.shared_snapshot_identity() != row['snapshot_identity']):
            raise ValueError('platform positive path changed original host/disk/input identity')
        remote_service(lab, request, 'restore')
        row['passed'] = True; lab.write_private(path, row)
    print('LONGHORN_PLATFORM_BUSINESS_PASS', flush=True)


def finish(lab):
    row = journal(lab)
    if not row['passed']:
        raise ValueError('platform module has no complete business result')
    empty(lab); lab.installation_health(); propagation(lab)
    if ((lab.DIRECTORY/'config.yml').read_text() != row['original_config'] or
            host_observation(lab) != row['hosts'] or lab.shared_snapshot_identity() != row['snapshot_identity']):
        raise ValueError('platform final baseline identity differs')
    remote_service(lab, row['service'], 'restore')
    (lab.DIRECTORY/RECORD).unlink()
    print('LONGHORN_PLATFORM_FIXTURES_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('business', 'clean', 'finish', 'propagation'))
    parser.add_argument('--relay-pid', type=int, default=0)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('longhorn_platform_lab', Path(__file__).with_name('longhorn-lab.py'))
    lab = importlib.util.module_from_spec(spec); sys.modules[spec.name] = lab; spec.loader.exec_module(lab)
    lab.require_lease()
    if args.action == 'propagation':
        lab.installation_health(); empty(lab)
        print('LONGHORN_PLATFORM_PROPAGATION_VERIFIED '+json.dumps(propagation(lab), sort_keys=True), flush=True)
    elif args.action == 'business':
        business(lab, args.relay_pid)
    elif args.action == 'clean':
        clean(lab, args.relay_pid, failed=True)
    else:
        finish(lab)


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise SystemExit(128+signum)
    signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
    main()
