"""Fixed Harbor lab fixtures and cleanup; never an installation entry point.

The controller retains the shared six-node lease. This helper additionally
binds every operation to the exact node owner and its private run directory.
Installation, upgrade, logical import and restore remain kubecli operations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

if __package__:
    from .harbor_fixture import Ledger, private_json, read_private
else:
    from harbor_fixture import Ledger, private_json, read_private


STATE = Path('/var/tmp/kubeauto-harbor-v1366')
DATA = Path('/var/data')
TLS = Path('/etc/kubernetes/ssl')
HOSTS = {'192.168.122.217', '192.168.122.243'}
SERVICES = {'log', 'registry', 'registryctl', 'postgresql', 'core', 'portal',
            'jobservice', 'redis', 'proxy', 'trivy-adapter'}


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError('Harbor node command refused: ' + args[0])
    return result.stdout


def guard(run_id):
    if not re.fullmatch(r'v1366-harbor-[a-z0-9-]{1,32}', run_id):
        raise ValueError('fixed Harbor run identity required')
    addresses = set(command('hostname', '-I').split())
    if len(addresses & HOSTS) != 1 or os.geteuid() != 0:
        raise ValueError('fixed leased Harbor node required')
    marker = Path('/var/tmp/kubeauto-v1366-node-owner')
    if marker.is_symlink() or marker.stat().st_uid != 0 or marker.stat().st_mode & 0o777 != 0o600:
        raise ValueError('private node ownership marker required')
    if marker.read_text().strip() != run_id:
        raise ValueError('node belongs to another run')
    return next(iter(addresses & HOSTS))


def initialize(run_id):
    host = guard(run_id)
    if STATE.exists() or STATE.is_symlink():
        raise ValueError('prior Harbor state must be inspected, never adopted')
    for path in [DATA, Path('/var/log/harbor')]:
        if path.exists() or path.is_symlink():
            raise ValueError('foreign Harbor data or logs exist')
    if platform.machine() != 'x86_64':
        raise ValueError('this focused path qualifies native AMD64 only')
    memory = int(re.search(r'^MemTotal:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)[1])
    disk = shutil.disk_usage(DATA.parent)
    if memory < 4 * 1024**2 or disk.total < 40 * 1024**3 or disk.free < 20 * 1024**3:
        raise ValueError('Harbor minimum memory or migration disk budget unavailable')
    STATE.mkdir(mode=0o700)
    private_json(STATE/'owner.json', {'run_id': run_id, 'host': host,
                 'machine': hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()})
    for name in ['harbor_node.py', 'harbor_fixture.py']:
        shutil.copy2(Path(__file__).parent/name, STATE/name)
        (STATE/name).chmod(0o600)
    return {'marker': 'HARBOR_NODE_ADMITTED', 'host': host, 'architecture': platform.machine()}


def owner(run_id):
    host = guard(run_id)
    if STATE.is_symlink() or STATE.stat().st_uid != 0 or STATE.stat().st_mode & 0o777 != 0o700:
        raise ValueError('private Harbor state required')
    record = read_private(STATE/'owner.json')
    if (record['run_id'] != run_id or record['host'] != host or
            record['machine'] != hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()):
        raise ValueError('Harbor ownership or machine identity changed')


def owned_containers():
    ids = command('/usr/local/bin/docker', 'ps', '-aq').split()
    if not ids:
        return []
    rows = json.loads(command('/usr/local/bin/docker', 'inspect', *ids))
    for row in rows:
        labels = row['Config'].get('Labels') or {}
        if (labels.get('com.docker.compose.project.working_dir') != str(DATA/'harbor') or
                labels.get('com.docker.compose.service') not in SERVICES):
            raise ValueError('foreign container prevents Harbor fixture mutation or cleanup')
    return rows


def settings(payload):
    if set(payload) != {'hostname', 'port', 'password', 'ca', 'docker'}:
        raise ValueError('exact private fixture settings required')
    if (payload['hostname'] != 'harbor.upgrade.invalid' or payload['port'] != 8443 or
            payload['ca'] != str(TLS/'ca.pem') or payload['docker'] != '/usr/local/bin/docker'):
        raise ValueError('fixture endpoint escaped its fixed local boundary')
    trust = Path('/etc/docker/certs.d/harbor.upgrade.invalid:8443')
    if trust.exists() or trust.is_symlink():
        raise ValueError('fixture will not adopt existing Docker CA trust')
    daemon = json.loads(Path('/etc/docker/daemon.json').read_text())
    if daemon.get('insecure-registries'):
        raise ValueError('fixture must prove verified TLS, not insecure registry access')
    private_json(STATE/'settings.json', payload)
    trust.mkdir(mode=0o700, parents=True)
    shutil.copy2(TLS/'ca.pem', trust/'ca.crt')
    return {'marker': 'HARBOR_FIXTURE_TLS_READY'}


def stop():
    rows = owned_containers()
    if rows:
        command('/usr/local/bin/docker', 'stop', '--time', '60', *[r['Id'] for r in rows])
    if any(r['State']['Running'] for r in owned_containers()):
        raise ValueError('Harbor source still accepts writes')
    return {'marker': 'HARBOR_FIXTURE_STOPPED'}


def clean_data():
    rows = owned_containers()
    if any(r['State']['Running'] for r in rows):
        raise ValueError('stop the owned Harbor instance before cleanup')
    if rows:
        command('/usr/local/bin/docker', 'rm', *[r['Id'] for r in rows])
    if owned_containers():
        raise ValueError('Harbor container cleanup incomplete')
    candidates = [DATA, Path('/var/log/harbor')]
    # Only quarantines named by this run's private product checkpoints qualify.
    for checkpoint in STATE.glob('backup-*/receipt.json'):
        receipt = read_private(checkpoint)
        if receipt.get('data_directory') != str(DATA):
            raise ValueError('checkpoint data owner differs')
        if receipt.get('quarantine'):
            quarantine = Path(receipt['quarantine'])
            if quarantine.parent != DATA.parent or not quarantine.name.startswith(('data.before-restore-', 'data.portable-aborted-')):
                raise ValueError('checkpoint quarantine escaped the owned data boundary')
            candidates.append(quarantine)
    for path in candidates:
        if path.is_symlink():
            raise ValueError('linked Harbor data path cannot be cleaned')
        if path.exists():
            if os.path.ismount(path) or any(os.path.ismount(p) for p in path.rglob('*') if p.is_dir()):
                raise ValueError('mounted Harbor data cannot be cleaned')
            shutil.rmtree(path)
    if any(path.exists() for path in candidates):
        raise ValueError('Harbor data cleanup incomplete')
    return {'marker': 'HARBOR_DATA_CLEAN_VERIFY_PASS'}


def clean_state(payload):
    if any(p.exists() or p.is_symlink() for p in [DATA, Path('/var/log/harbor')]):
        raise ValueError('Harbor data cleanup must precede archive cleanup')
    for unit in ['docker', 'containerd']:
        if command('systemctl', 'show', unit, '--property=ActiveState', '--value').strip() != 'inactive':
            raise ValueError('runtime cleanup must precede archive cleanup')
    paths=list(STATE.rglob('*'))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in paths):
        raise ValueError('archive contains an unexpected file type')
    actual = {}
    for path in paths:
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            actual[str(path.relative_to(STATE))] = digest.hexdigest()
    if not actual or payload != actual:
        raise ValueError('durable controller archive differs; retain original recovery files')
    shutil.rmtree(STATE)
    return {'marker': 'HARBOR_NODE_CLEAN_VERIFY_PASS', 'archived_files': len(actual)}


def runtime_clean_admission():
    # This fixture installed Compose/Docker only. Never use a Kubernetes
    # destroy request (or erase Kubernetes data) to clean this boundary.
    for unit in ['kubelet','kube-proxy','kube-apiserver','kube-controller-manager',
                 'kube-scheduler','kube-lb','etcd']:
        active=command('systemctl','show',unit,'--property=ActiveState','--value').strip()
        if active not in ['inactive','unknown']:
            raise ValueError('Kubernetes service prevents Harbor-only runtime cleanup')
    for path in ['/var/lib/etcd','/var/lib/kubelet','/var/lib/kube-proxy',
                 '/etc/kubernetes/manifests','/etc/cni','/opt/local-path-provisioner']:
        if Path(path).exists() or Path(path).is_symlink():
            raise ValueError('Kubernetes data prevents Harbor-only runtime cleanup')
    if owned_containers():
        raise ValueError('owned Harbor data/container cleanup must finish before runtime cleanup')
    return {'marker':'HARBOR_RUNTIME_ONLY_CLEAN_ADMISSION_PASS'}


def transfer_path(label):
    if label not in ['transfer-213','transfer-216']:
        raise ValueError('fixed logical transfer identity required')
    return STATE/label


def export_files(backup):
    if backup not in ['backup-portable213','backup-portable216']:
        raise ValueError('fixed logical source checkpoint required')
    directory=STATE/backup
    manifest=read_private(directory/'portable.json')
    files=['portable.json',*manifest['files']]
    for name in files:
        if Path(name).name!=name or name in ['.','..']:
            raise ValueError('logical export file escaped its checkpoint')
        path=directory/name
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
            raise ValueError('private complete logical export file required')
        if name!='portable.json':
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
            if digest.hexdigest()!=manifest['files'][name]:
                raise ValueError('logical export bytes changed')
    if any(row['State']['Running'] for row in owned_containers()):
        raise ValueError('logical export source must remain stopped')
    return files


def retire_fixture():
    if DATA.exists() or DATA.is_symlink() or owned_containers():
        raise ValueError('fixture retirement requires stopped and removed owned target data')
    directory=STATE/'previous-target-fixture'
    if directory.exists() or directory.is_symlink():
        raise ValueError('previous target fixture already retained')
    for name in ['settings.json','ledger.json']:
        read_private(STATE/name)
    # Only files delivered by this fixed setup11 fixture are retired. No
    # arbitrary CA tree or runtime trust is removed.
    for name in ['ca.pem','harbor.pem','harbor-key.pem']:
        source=TLS/name
        if source.is_symlink() or not source.is_file():
            raise ValueError('original owned target TLS fixture required')
    trust=Path('/etc/docker/certs.d/harbor.upgrade.invalid:8443')
    if trust.is_symlink() or {p.name for p in trust.iterdir()}!={'ca.crt'}:
        raise ValueError('foreign target trust prevents fixture retirement')
    directory.mkdir(mode=0o700)
    for name in ['settings.json','ledger.json']:(STATE/name).rename(directory/name)
    for name in ['ca.pem','harbor.pem','harbor-key.pem']:(TLS/name).rename(directory/name)
    trust.rename(directory/'docker-trust')
    return {'marker':'HARBOR_TARGET_FIXTURE_RETIRED'}


def retain_restore_quarantine():
    checkpoint=STATE/'backup-original/receipt.json'
    if not checkpoint.exists():return {'marker':'HARBOR_PRIOR_RESTORE_NOT_APPLICABLE'}
    record=read_private(checkpoint)
    if record.get('phase')!='restored' or record.get('data_directory')!=str(DATA):
        raise ValueError('completed owned original restore required')
    source=Path(record['quarantine']);target=STATE/'retained-restored-target'
    if (source.parent!=DATA.parent or not source.name.startswith('data.before-restore-') or
            source.is_symlink() or not source.is_dir() or target.exists() or target.is_symlink()):
        raise ValueError('exact owned restore quarantine required')
    source.rename(target)
    return {'marker':'HARBOR_PRIOR_RESTORE_QUARANTINE_RETAINED'}


def transfer_operation(action,payload):
    if action=='export-files':return {'files':export_files(payload['backup'])}
    directory=transfer_path(payload['label'])
    if action=='transfer-directory':
        directory.mkdir(mode=0o700)
    elif action=='link-transfer':
        files=export_files(payload['backup'])
        for name in files:os.link(STATE/payload['backup']/name,directory/name)
    elif action=='transfer-fixture':
        if set(payload['files'])!={'settings.json','ledger.json'}:
            raise ValueError('only the original verification fixture can be transferred')
        for name,content in payload['files'].items():
            private_json(directory/('fixture-'+name),json.loads(content))
    elif action=='adopt-fixture':
        if (STATE/'settings.json').exists() or (STATE/'ledger.json').exists():
            raise ValueError('logical target cannot replace an active verification identity')
        settings(read_private(directory/'fixture-settings.json'))
        private_json(STATE/'ledger.json',read_private(directory/'fixture-ledger.json'))
    else:raise ValueError('unknown logical fixture operation')
    return {'marker':'HARBOR_TRANSFER_FIXTURE_PASS'}


def execute(action, run_id, payload=None):
    if action == 'init':
        return initialize(run_id)
    owner(run_id)
    if action == 'settings':
        return settings(payload)
    if action in ['create', 'verify']:
        owned_containers()
        ledger = Ledger(read_private(STATE/'settings.json'), STATE)
        return getattr(ledger, action)()
    if action == 'configuration':
        # Only the controller's captured private pipe consumes this response.
        return {'configuration': (DATA/'harbor/harbor.yml').read_text()}
    if action == 'stop':
        return stop()
    if action == 'clean-data':
        return clean_data()
    if action == 'inventory':
        return {'architecture': platform.machine(), 'containers': owned_containers()}
    if action == 'clean-state':
        return clean_state(payload)
    if action == 'runtime-clean-admission':
        return runtime_clean_admission()
    if action == 'retire-fixture':return retire_fixture()
    if action == 'retain-restore-quarantine':return retain_restore_quarantine()
    if action in ['export-files','transfer-directory','link-transfer','transfer-fixture','adopt-fixture']:
        return transfer_operation(action,payload)
    raise ValueError('unknown fixed Harbor node operation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'settings', 'create', 'verify',
                                        'configuration', 'stop', 'clean-data', 'clean-state', 'inventory',
                                        'runtime-clean-admission','export-files','transfer-directory',
                                        'link-transfer','transfer-fixture','adopt-fixture','retire-fixture',
                                        'retain-restore-quarantine'])
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin) if args.action in ['settings','clean-state','export-files',
            'transfer-directory','link-transfer','transfer-fixture','adopt-fixture'] else None
        print(json.dumps(execute(args.action, args.run_id, payload)))
    except Exception:
        raise SystemExit('Harbor node operation refused; retain private inputs and inspect the owning boundary') from None


if __name__ == '__main__':
    main()
