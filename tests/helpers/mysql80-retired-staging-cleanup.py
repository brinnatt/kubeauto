#!/usr/bin/env python3
"""Remove only finalized MySQL80 staging whose raw evidence is already local."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
HOST = 'root@192.168.47.131'


def archived_run(directory):
    directory = directory.resolve()
    plan = json.loads((directory/'plan.json').read_text())
    ident = plan['id']
    if (directory.parent != ROOT/'logs' or not re.fullmatch('[a-f0-9]{12}', ident)
            or directory.name != 'mysql80-'+plan['stage']+'-rocky8-'+ident
            or plan['os'] != '8.10' or plan['base'] != '/tmp/mb80-'+ident):
        raise ValueError('MYSQL80_RETIRED_SCOPE_REFUSED')
    names = ['plan.json', 'console.log', 'state.exit', 'state.finalized']
    for name in ('state.exit', 'state.finalized'):
        int((directory/name).read_text().strip())
    for name in ('evidence.tar.gz', 'dockerd.log'):
        if (directory/name).is_file():
            names.append(name)
    return {'base': plan['base'], 'files': {name: hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in names}}


REMOTE = '''
import hashlib,json,os,pathlib,shutil,subprocess,sys
request=json.load(sys.stdin)
assert pathlib.Path('/etc/rocky-release').read_text().startswith('Rocky Linux release 8.10')
for service in ('docker','containerd','kubelet'):
    assert subprocess.run(['systemctl','is-active','--quiet',service]).returncode!=0
for record in request:
    base=pathlib.Path(record['base'])
    if not base.exists():continue
    assert not base.is_symlink() and base.parent==pathlib.Path('/tmp')
    for proc in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
        try: command=proc.read_bytes()
        except (OSError,ProcessLookupError):continue
        assert str(base).encode() not in command
    assert str(base) not in pathlib.Path('/proc/self/mountinfo').read_text()
    assert not (base/'runtime').exists() and not (base/'fixture').exists()
    for name,expected in record['files'].items():
        assert hashlib.sha256((base/name).read_bytes()).hexdigest()==expected,(base,name)
    for name in ('evidence.tar.gz','dockerd.log'):
        assert not (base/name).exists() or name in record['files']
for record in request:
    base=pathlib.Path(record['base'])
    if base.exists():shutil.rmtree(base)
    assert not base.exists()
subprocess.run(['systemctl','start','tuned'],check=True)
subprocess.run(['systemctl','is-active','--quiet','tuned'],check=True)
print('MYSQL80_RETIRED_STAGING_CLEAN_PASS count='+str(len(request)))
print('MYSQL80_ORIGINAL_TUNED_ACTIVE_RESTORED')
'''


def main():
    reserve = (ROOT/'logs/mysql80-rocky8-capacity-reserve-20261125.log').read_text()
    if not reserve.startswith('active\n'):
        raise ValueError('MYSQL80_ORIGINAL_SERVICE_STATE_MISSING')
    directories = [p for p in (ROOT/'logs').glob('mysql80-*-rocky8-*') if (p/'plan.json').is_file()]
    records = [archived_run(p) for p in directories]
    # stdin carries data only. The script is fixed; both original fixture locks
    # and the shared node lock fence the entire verify/remove/restore operation.
    import shlex
    command = ('flock -n /run/lock/kubeauto-core-pool.lock '
               'flock -n /var/lock/kubeauto-starrocks-compose.lock '
               'flock -n /var/lock/kubeauto-mysql-backup-release.lock python3 -c '+shlex.quote(REMOTE))
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', HOST, command],
                   input=json.dumps(records), text=True, check=True, timeout=60)


if __name__ == '__main__':
    main()
