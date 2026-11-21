"""Focused native-AMD64 Harbor qualification on the fixed leased upgrade pool.

Reuse the existing durable command, control isolation, six-node leases and
ownership gates. This path does not install or upgrade a Kubernetes cluster.
It cannot establish native ARM64 execution or sign a complete parent matrix.
"""
from __future__ import annotations

import argparse
import fcntl
from datetime import datetime, timezone
import io
import inspect
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tarfile

import yaml

from upgrade.lab.next_session import (Session, ROOT, STATE, SOURCE_COMMIT,
                                      phase_log, sha, write)
from upgrade.product.launcher import admit_static
from upgrade.product.hop import MATRIX_PATH


SOURCE_HOST = '192.168.122.217'
TARGET_HOST = '192.168.122.243'
NODE_STATE = '/var/tmp/kubeauto-harbor-v1366'
PHASES = ['harbor-source', 'harbor-migration', 'harbor-restore', 'harbor-new-install', 'harbor-clean']
PORTABLE_PHASES = ['harbor-source', 'harbor-portable', 'harbor-clean']
PACKAGES = {'v2.14.6': 'harbor-offline-installer-v2.14.6.tgz',
            'v2.16.0': 'harbor-offline-installer-v2.16.0.tgz'}
PROC = Path('/proc')


def paused_package_boundary(session, parent, pid, *, supplied=False):
    """Only supply frozen fixtures to a verified stopped final supervisor."""
    from upgrade.lab.next_session import LOCKS, LOCK_DIRECTORY, verify_inputs
    pause=json.loads((session.evidence/'preemptive-fixture-pause.json').read_text())
    if (pause.get('run_id')!=parent or pause.get('child')!=session.run_id or
            pause.get('supervisor_pid')!=pid or pause.get('signal')!='SIGSTOP'):
        raise ValueError('exact recorded paused supervisor required')
    process=PROC/str(pid)
    expected=['/usr/local/kubeauto/.venv/bin/python','-B','-m','upgrade.lab.next_session',
              'run','--mode','final','--run-id',parent]
    if (process.joinpath('cmdline').read_bytes().split(b'\0')[:-1]!=[x.encode() for x in expected] or
            not any(line.startswith('State:') and line.split()[1]=='T'
                    for line in process.joinpath('status').read_text().splitlines())):
        raise ValueError('original final supervisor must remain stopped')
    if session.mode!='final' or json.loads(session.owner.read_text())!=session.plan:
        raise ValueError('original final Harbor owner required')
    for descriptor,name in LOCKS.items():
        path=LOCK_DIRECTORY/name
        if (process/'fd'/str(descriptor)).stat().st_ino!=path.stat().st_ino:
            raise ValueError('paused supervisor lease identity differs')
        with path.open('r') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:pass
            else:raise ValueError('paused supervisor must retain all shared leases')
    for phase in ['harbor-source','harbor-migration','harbor-restore','harbor-new-install']:
        record=json.loads((session.evidence/(phase+'.status.json')).read_text())
        if record.get('rc')!=0 or record.get('attempt')!=1:
            raise ValueError('accepted original Harbor prerequisite required')
        for suffix in ['exit','finalized']:
            if (session.evidence/(phase+'.attempt-1.'+suffix)).read_text().strip()!='0':
                raise ValueError('durable Harbor prerequisite required')
    if ((session.evidence/'harbor-portable.status.json').exists() or
            (session.evidence/'paused-package-supply.json').exists()!=supplied):
        raise ValueError('only the recorded pre-import fixture gap may be supplied once')
    verify_inputs(STATE,session.target)
    for target in [False,True]:
        base=session.target if target else session.source
        cluster=session.target_cluster if target else session.source_cluster
        config=base/'clusters'/cluster/'config.yml'
        record=json.loads((session.evidence/('target-config.json' if target else 'source-config.json')).read_text())
        if record!={'path':str(config),'sha256':sha(config)}:
            raise ValueError('original product configuration changed')


def supply_paused_packages(session, parent, pid):
    paused_package_boundary(session,parent,pid)
    probe=('import sys,json\nfrom pathlib import Path\n'
           'sys.path.insert(0,'+repr(NODE_STATE)+')\nfrom harbor_node import owner,owned_containers\n'
           'owner('+repr(session.run_id)+')\n'
           's=Path('+repr(NODE_STATE)+')\n')
    session.remote(SOURCE_HOST,['python3','-B','-'],data=probe+
        'r=json.loads((s/"backup-portable213/receipt.json").read_text())\n'
        'assert r["phase"]=="backup-complete" and r["source"]=="v2.13.0"\n'
        'assert not any(x["State"]["Running"] for x in owned_containers())\n')
    session.remote(TARGET_HOST,['python3','-B','-'],data=probe+
        'assert not owned_containers()\nassert not Path("/var/data").exists()\n'
        'assert not list(s.glob("backup-import*"))\n')
    session.packages(TARGET_HOST)
    # Preserve the additional fixture preparation and exact repair helper;
    # do not replace the active checkout, manifest, logs or original result.
    from upgrade.lab.harbor_inputs import PACKAGES as checksums
    write(session.evidence/'paused-package-supply.json',{
        'parent':parent,'child':session.run_id,'supervisor_pid':pid,
        'helper_sha256':sha(Path(__file__)),
        'packages':{PACKAGES[v]:checksums[v] for v in PACKAGES},
        'mutation':'private test-package fixtures only; active staged inputs unchanged',
        'at':datetime.now(timezone.utc).isoformat()})
    print('HARBOR_PAUSED_PACKAGE_SUPPLY_PASS supervisor=stopped lease=held',flush=True)


def resume_supplied_supervisor(session, parent, pid):
    paused_package_boundary(session,parent,pid,supplied=True)
    record=json.loads((session.evidence/'paused-package-supply.json').read_text())
    from upgrade.lab.harbor_inputs import PACKAGES as checksums
    if (record['parent']!=parent or record['child']!=session.run_id or
            record['supervisor_pid']!=pid or record['helper_sha256']!=sha(Path(__file__)) or
            record['packages']!={PACKAGES[v]:checksums[v] for v in PACKAGES}):
        raise ValueError('completed exact fixture supply required')
    os.kill(pid,signal.SIGCONT)
    write(session.evidence/'paused-package-resume.json',{
        'parent':parent,'child':session.run_id,'supervisor_pid':pid,
        'at':datetime.now(timezone.utc).isoformat(),'signal':'SIGCONT'})
    print('HARBOR_SUPPLIED_SUPERVISOR_RESUMED lease=held',flush=True)


def plan(run_id, mode='qualify'):
    import re
    if not re.fullmatch(r'v1366-harbor-[a-z0-9-]{1,32}', run_id):
        raise ValueError('fixed Harbor run identity required')
    if mode not in ['qualify', 'portable', 'final']:
        raise ValueError('fixed Harbor mode required')
    required=range(1,15) if mode=='final' else range(1,5)
    admit_static(MATRIX_PATH, required=[f'UPG-{n:02}' for n in required])
    matrix=yaml.safe_load(MATRIX_PATH.read_text())
    if matrix['meta'].get('execution_enabled') is not True or not matrix['meta'].get('runner'):
        raise ValueError('upgrade execution must be explicitly enabled and bound')
    phases=PORTABLE_PHASES if mode=='portable' else PHASES
    if mode=='final':phases=PHASES[:-1]+['harbor-portable',PHASES[-1]]
    key={'qualify':'harbor_native_amd64_qualification','portable':'harbor_logical_qualification',
         'final':'harbor_final_acceptance'}[mode]
    scope=yaml.safe_load((MATRIX_PATH.parent/'acceptance-plan.yaml').read_text()).get(key,{})
    if (scope.get('enabled') is not True or scope.get('runner')!='upgrade/lab/harbor-session.sh' or
            scope.get('phases')!=phases or scope.get('active_hosts')!=[SOURCE_HOST,TARGET_HOST]):
        raise ValueError('focused Harbor scope must be frozen before node admission')
    token = __import__('hashlib').sha256(run_id.encode()).hexdigest()[:8]
    return {'run_id': run_id, 'mode': mode, 'source_commit': SOURCE_COMMIT,
            'source': 'v1.35.10', 'target': 'v1.36.6',
            'clusters': ['upgrade-harbor-source-'+token, 'upgrade-harbor-target-'+token],
            'phases': phases, 'builds': [], 'kubernetes_mutation': 'none',
            'leased_hosts': ['192.168.122.'+h for h in ['217','243','246','193','210','216']],
            'active_hosts': [SOURCE_HOST,TARGET_HOST],
            'architecture': 'native-amd64-only; ARM64 provenance/static checks without native execution',
            'entry': 'config.yml + kubecli setup 01/03/11 and upgrade --harbor',
            'cleanup': 'explicit owned cleanup after evidence collection; failures retain shared leases'}


class HarborSession(Session):
    def __init__(self, run_id, mode='qualify'):
        super().__init__('qualify', run_id)
        self.mode=mode
        self.plan = plan(run_id,mode)
        self.source_cluster, self.target_cluster = self.plan['clusters']
        self.archive_directory=Path('/home/kubeauto-v1366-archives')/run_id

    def owner_check(self):
        from upgrade.lab.harbor_inputs import verify_payloads
        verify_payloads(STATE)
        parent=self.archive_directory.parent
        if parent.is_symlink() or self.archive_directory.exists() or self.archive_directory.is_symlink():
            raise ValueError('fresh private archive directory required')
        parent.mkdir(mode=0o700,exist_ok=True)
        if parent.stat().st_uid!=0 or parent.stat().st_mode & 0o777 != 0o700:
            raise ValueError('private root-owned archive parent required')
        if __import__('shutil').disk_usage(parent).free < 12*1024**3:
            raise ValueError('Harbor recovery archive capacity below 12GiB budget')
        super().owner_check()
        self.archive_directory.mkdir(mode=0o700)

    def remote(self, host, arguments, *, data=None):
        if host not in {SOURCE_HOST, TARGET_HOST}:
            raise ValueError('host escaped fixed Harbor pool')
        return self.command(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            'root@'+host, shlex.join([str(a) for a in arguments])],
            data=data, capture=True)

    def node(self, action, *, host=SOURCE_HOST, payload=None):
        if action == 'runtime-clean-admission':
            from upgrade.lab.harbor_node import runtime_clean_admission
            # Read-only probe through stdin: do not replace the node helper
            # already bound to the complete private recovery archive.
            program=('import sys,json\nfrom pathlib import Path\n'
                     'sys.path.insert(0,'+repr(NODE_STATE)+')\n'
                     'from harbor_node import owner,command,owned_containers\n'
                     'owner('+repr(self.run_id)+')\n'+
                     inspect.getsource(runtime_clean_admission)+
                     '\nprint(json.dumps(runtime_clean_admission()))\n')
            return json.loads(self.remote(host,['python3','-B','-'],data=program))
        raw = self.remote(host, ['python3', '-B', NODE_STATE+'/harbor_node.py',
            action, '--run-id', self.run_id],
            data=json.dumps(payload) if payload is not None else None)
        return json.loads(raw)

    def bootstrap(self, host):
        # Only these two repo-owned scripts enter a remote TemporaryDirectory.
        # The helper checks node ownership/capacity before creating durable state.
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for name in ['harbor_node.py', 'harbor_fixture.py']:
                archive.add(ROOT/'upgrade/lab'/name, arcname=name)
        program = ('import subprocess,sys,tarfile,tempfile; '
                   't=tempfile.TemporaryDirectory(prefix="kubeauto-harbor-bootstrap-"); '
                   'a=tarfile.open(fileobj=sys.stdin.buffer,mode="r|*"); '
                   'a.extractall(t.name); a.close(); '
                   'r=subprocess.run([sys.executable,"-B",t.name+"/harbor_node.py","init","--run-id",sys.argv[1]]); '
                   't.cleanup(); sys.exit(r.returncode)')
        # Session.command uses text I/O. A temporary private tar lets SSH stream
        # bytes without encoding them or exposing private fixture input.
        path=self.evidence/('bootstrap-'+host+'.tar')
        with path.open('xb') as file:file.write(stream.getvalue())
        path.chmod(0o600)
        with path.open('rb') as file:
            result=subprocess.run(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=10',
                'root@'+host,shlex.join(['python3','-B','-c',program,self.run_id])],
                stdin=file,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
        if result.returncode:
            raise ValueError('Harbor node bootstrap refused; no installation permitted')
        write(self.evidence/('admission-'+host+'.json'),json.loads(result.stdout))

    def configure(self, target=False):
        self.select(target)
        self.cli(self.base, self.base, 'new', self.cluster)
        directory=self.base/'clusters'/self.cluster
        host=TARGET_HOST if target else SOURCE_HOST
        text=(self.base/'conf/hosts.allinone').read_text().replace('192.168.1.1',host)
        text=text.replace('#192.168.1.8 NEW_INSTALL=false',host+' NEW_INSTALL=true')
        text=text.replace('base_dir="/usr/local/kubeauto/"','base_dir='+shlex.quote(str(self.base)))
        text=text.replace('_cluster_name_',self.cluster)
        text=text.replace('CONTAINER_RUNTIME="containerd"','CONTAINER_RUNTIME="docker"')
        text=text.replace("k8s_nodename='master-aio'","k8s_nodename='upgrade-harbor-"+host.rsplit('.',1)[1]+"'")
        (directory/'hosts').write_text(text)
        configuration=yaml.safe_load((directory/'config.yml').read_text())
        configuration.update(KUBE_RESERVED_ENABLED='no',SYS_RESERVED_ENABLED='no',
            REGISTRY_HOST_IP='192.168.122.2',INSECURE_REG=[],ENABLE_MIRROR_REGISTRY=False,
            HARBOR_DOMAIN='harbor.upgrade.invalid',HARBOR_PATH='/var/data',
            HARBOR_TLS_PORT=8443,HARBOR_WITH_TRIVY=False)
        (directory/'config.yml').write_text(yaml.safe_dump(configuration,allow_unicode=True,sort_keys=False))
        (directory/'config.yml').chmod(0o600)
        write(self.evidence/('target-config.json' if target else 'source-config.json'),
              {'path':str(directory/'config.yml'),'sha256':sha(directory/'config.yml')})

    def packages(self, host):
        # Product reads exact already-qualified local offline packages. No pull,
        # build, external download or mutable publication occurs in this path.
        from upgrade.lab.harbor_inputs import PACKAGES as checksums
        if host not in {SOURCE_HOST,TARGET_HOST}:
            raise ValueError('package destination escaped fixed Harbor pool')
        for version,name in PACKAGES.items():
            source=self.target/'down'/name
            if source.is_symlink() or sha(source)!=checksums[version]:
                raise ValueError('qualified original Harbor package required')
            program=('import sys,hashlib,os\nfrom pathlib import Path\n'
                'sys.path.insert(0,'+repr(NODE_STATE)+')\nfrom harbor_node import owner\n'
                'owner('+repr(self.run_id)+')\n'
                'p=Path('+repr(NODE_STATE+'/'+name)+'); t=p.with_name(p.name+".pending")\n'
                'def digest(p):\n'
                ' h=hashlib.sha256()\n'
                ' with p.open("rb") as f:\n'
                '  for b in iter(lambda:f.read(1024*1024),b""):h.update(b)\n'
                ' return h.hexdigest()\n'
                'assert not p.is_symlink() and not t.is_symlink()\n'
                'assert not p.exists() or (p.is_file() and digest(p)=='+repr(checksums[version])+')\n')
            # Refuse foreign bytes and partial prior transfers before writing.
            self.remote(host,['python3','-B','-'],data=program+'assert not t.exists()\n')
            self.command(['rsync','-a','--chmod=F600',str(source),
                          'root@'+host+':'+NODE_STATE+'/'+name+'.pending'])
            self.remote(host,['python3','-B','-'],data=program+
                'assert t.is_file() and digest(t)=='+repr(checksums[version])+'\n'
                'with t.open("rb") as f:os.fsync(f.fileno())\n'
                'os.replace(t,p)\n')

    def maintenance(self, operation, *, backup='backup-original', target=False, transfer=None):
        self.select(target)
        args=['upgrade',self.cluster,'--harbor',operation,'--harbor-backup-dir',NODE_STATE+'/'+backup]
        if operation=='backup':args+=['--harbor-ca-file','/etc/kubernetes/ssl/ca.pem']
        if operation in ['upgrade','import']:
            args+=['--harbor-bridge-package',NODE_STATE+'/'+PACKAGES['v2.14.6'],
                   '--harbor-target-package',NODE_STATE+'/'+PACKAGES['v2.16.0']]
        if transfer:args+=['--harbor-transfer-dir',NODE_STATE+'/'+transfer]
        self.cli(self.base,self.target,*args)

    def transfer_export(self, backup, label, *, same_host=False):
        source=TARGET_HOST if same_host else SOURCE_HOST
        files=self.node('export-files',host=source,payload={'backup':backup})['files']
        self.node('transfer-directory',host=TARGET_HOST,payload={'label':label})
        destination=NODE_STATE+'/'+label
        if same_host:
            self.node('link-transfer',host=TARGET_HOST,payload={'backup':backup,'label':label})
        else:
            # Stream private bytes directly between the two leased hosts. No
            # large recovery payload enters the controller root filesystem.
            ssh=['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=10']
            read=shlex.join(ssh+['root@'+source,shlex.join(['tar','-C',NODE_STATE+'/'+backup,'-cf','-',*files])])
            write_command=shlex.join(ssh+['root@'+TARGET_HOST,shlex.join(['tar','-C',destination,'-xf','-'])])
            self.command(['bash','-o','pipefail','-c',read+' | '+write_command])
        # The ledger and credentials are verification fixtures, never used to
        # recreate users/roles or repair target database contents.
        ledger=self.remote(SOURCE_HOST,['python3','-B','-c',
            'from pathlib import Path; import json; p=Path('+repr(NODE_STATE)+'); '
            'print(json.dumps({n:(p/n).read_text() for n in ["settings.json","ledger.json"]}))'])
        self.node('transfer-fixture',host=TARGET_HOST,payload={'label':label,'files':json.loads(ledger)})

    def portable_phase(self, *, resume_export=False, resume_backup=False):
        if resume_backup:
            self.repair_portable_boundary(stopped_backup=True)
            return self.finish_portable()
        if resume_export:
            self.repair_portable_boundary()
        elif not (self.evidence/'target-config.json').exists():
            self.host_operation('marked')
            self.bootstrap(TARGET_HOST)
            self.configure(target=True)
            for stage in ['01','03']:
                self.cli(self.target,self.target,'setup',self.cluster,stage)
        else:
            # Final acceptance has already proved clean target setup/idempotence.
            self.node('stop',host=TARGET_HOST)
            self.node('clean-data',host=TARGET_HOST)
            self.node('retire-fixture',host=TARGET_HOST)
        if not resume_export:
            # Both the standalone and final new-install handoff need these
            # exact offline bridge/target inputs before source writes stop.
            self.packages(TARGET_HOST)
        # Keep the previous restore's isolated target tree in the private
        # recovery archive, freeing its fixed quarantine name for a new,
        # independently checkpointed portable-source backup/restore cycle.
        if not resume_export:
            self.node('retain-restore-quarantine')
            self.maintenance('backup',backup='backup-portable213')
        self.maintenance('export',backup='backup-portable213')
        self.transfer_export('backup-portable213','transfer-213')
        self.maintenance('import',backup='backup-import213',target=True,transfer='transfer-213')
        self.node('adopt-fixture',host=TARGET_HOST,payload={'label':'transfer-213'})
        self.record_node('portable213-imported',host=TARGET_HOST)
        self.assert_source_stopped()
        return self.finish_portable()

    def finish_portable(self):
        self.maintenance('backup',backup='backup-portable216',target=True)
        self.maintenance('export',backup='backup-portable216',target=True)
        self.transfer_export('backup-portable216','transfer-216',same_host=True)
        self.maintenance('abort-import',backup='backup-import213',target=True)
        self.maintenance('import',backup='backup-import216',target=True,transfer='transfer-216')
        self.record_node('portable216-imported',host=TARGET_HOST)
        self.assert_source_stopped()
        self.maintenance('abort-import',backup='backup-import216',target=True)
        self.maintenance('abort-import',backup='backup-import216',target=True)
        self.maintenance('restore',backup='backup-portable213')
        self.record_node('portable-source-restored')
        self.preserve('harbor-portable-target',1,host=TARGET_HOST)
        print('HARBOR_LOGICAL_213_BRIDGE_216_AND_216_RESTORE_PASS',flush=True)

    def repair_portable_boundary(self, *, stopped_backup=False):
        """Resume only the proven stopped export, never an unknown import."""
        if self.mode!='portable' or json.loads(self.owner.read_text())!=self.plan:
            raise ValueError('original portable supervisor ownership required')
        from upgrade.lab.next_session import LOCKS, LOCK_DIRECTORY
        for name in LOCKS.values():
            with (LOCK_DIRECTORY/name).open('r') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:raise ValueError('original supervisor must retain all shared leases')
        for suffix in ['exit','finalized']:
            if (self.evidence/('harbor-source.attempt-1.'+suffix)).read_text().strip()!='0':
                raise ValueError('accepted original source prerequisite missing')
        failed=json.loads((self.evidence/'harbor-portable.status.json').read_text())
        if failed.get('rc')!=1 or failed.get('attempt')!=1:
            raise ValueError('only the original first export failure can be resumed')
        for target in [False,True]:
            self.select(target)
            path=self.base/'clusters'/self.cluster/'config.yml'
            record=json.loads((self.evidence/('target-config.json' if target else 'source-config.json')).read_text())
            if record!={'path':str(path),'sha256':sha(path)}:
                raise ValueError('prepared Harbor configuration changed')
        self.host_operation('marked')
        self.assert_source_stopped()
        if stopped_backup:
            return self.stopped_backup_boundary()
        if self.node('inventory',host=TARGET_HOST)['containers']:
            raise ValueError('export repair requires the original empty target')
        # This read-only admission preserves the helper already bound to the
        # source recovery archive. The product revalidates archive hashes.
        program='''import json,sys
from pathlib import Path
sys.path.insert(0,''' + repr(NODE_STATE) + ''')
from harbor_node import owner
owner(''' + repr(self.run_id) + ''')
s=Path(''' + repr(NODE_STATE) + '''); b=s/'backup-portable213'
r=json.loads((b/'receipt.json').read_text()); f=json.loads((b/'first-failure.json').read_text())
expected=['/usr/local/bin/docker-compose','-f','/var/data/harbor/docker-compose.yml','up','-d','--no-deps','--wait','--wait-timeout','120','postgresql']
assert r['phase']=='portable-export-intent' and r['source']=='v2.13.0'
assert f['command']==expected and f['returncode']==1 and f['timed_out'] is False
assert 'failed to initialize logging driver' in (b/'commands.log').read_text()
assert not (b/'registry.dump').exists() and not (b/'portable.json').exists()
print('HARBOR_EXPORT_REPAIR_ADMISSION_PASS')
'''
        if self.remote(SOURCE_HOST,['python3','-B','-'],data=program).strip()!='HARBOR_EXPORT_REPAIR_ADMISSION_PASS':
            raise ValueError('original syslog export failure admission missing')
        program='''from pathlib import Path
assert not Path('/var/data/harbor').exists()
assert not Path('''+repr(NODE_STATE)+''','settings.json').exists()
print('HARBOR_EMPTY_TARGET_PASS')
'''
        if self.remote(TARGET_HOST,['python3','-B','-'],data=program).strip()!='HARBOR_EMPTY_TARGET_PASS':
            raise ValueError('original target preparation boundary changed')

    def stopped_backup_boundary(self):
        """Accept only the observed Valkey header failure, before any archive."""
        prefix=self.evidence/'harbor-portable-repair.attempt-1'
        for suffix in ['exit','finalized']:
            if Path(str(prefix)+'.'+suffix).read_text().strip()!='1':
                raise ValueError('original failed portable repair must be finalized')
        if 'the stopped queue has no valid RDB snapshot header' not in Path(str(prefix)+'.log').read_text():
            raise ValueError('only the proven Valkey RDB header failure can resume')
        accepted=json.loads((self.evidence/'portable213-imported-business.json').read_text())
        if accepted.get('marker')!='HARBOR_BUSINESS_LEDGER_PASS':
            raise ValueError('first logical import business ledger missing')
        inventory=self.node('inventory',host=TARGET_HOST)['containers']
        if not inventory or any(row['State']['Running'] for row in inventory):
            raise ValueError('logical target must retain the stopped backup boundary')
        program="""import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,"""+repr(NODE_STATE)+""")
from harbor_node import owner
owner("""+repr(self.run_id)+""")
s=Path("""+repr(NODE_STATE)+"""); b=s/'backup-portable216'
r=json.loads((b/'receipt.json').read_text())
assert r['phase']=='stopped' and r['source']=='v2.16.0' and r['files']=={} and r['tls']=={}
assert r['queue']['engine']=='valkey' and r['queue']['version']=='9.0.3'
assert not any((b/name).exists() or (b/name).is_symlink() for name in ['data.tar','images.tar','registry.dump','portable.json'])
with Path('/var/data/redis/dump.rdb').open('rb') as stream: assert stream.read(9)==b'VALKEY080'
i=json.loads((s/'backup-import213/receipt.json').read_text())
assert i['phase']=='v2.16.0-ready' and i['kind']=='harbor-portable-target'
ledger=json.loads((s/'ledger.json').read_text())
expected="""+repr(accepted)+"""
for key in ['project_id','artifact_id','digest','payload_sha256']:
    assert ledger[key]==expected[key]
assert not (s/'backup-import216').exists() and not (s/'transfer-216').exists()
print('HARBOR_STOPPED_VALKEY_REPAIR_ADMISSION_PASS')
"""
        if self.remote(TARGET_HOST,['python3','-B','-'],data=program).strip()!='HARBOR_STOPPED_VALKEY_REPAIR_ADMISSION_PASS':
            raise ValueError('stopped Valkey backup admission missing')

    def assert_source_stopped(self):
        rows=self.node('inventory')['containers']
        if not rows or any(row['State']['Running'] for row in rows):
            raise ValueError('logical source must retain its original stopped boundary')

    def record_node(self, label, *, host=SOURCE_HOST):
        write(self.evidence/(label+'-inventory.json'),self.node('inventory',host=host))
        write(self.evidence/(label+'-business.json'),self.node('verify',host=host))

    def source_phase(self):
        self.host_operation('admit')
        self.host_operation('claim')
        self.bootstrap(SOURCE_HOST)
        self.configure()
        self.packages(SOURCE_HOST)
        for stage in ['01','03','11']:
            self.cli(self.source,self.source,'setup',self.cluster,stage)
        original=yaml.safe_load(self.node('configuration')['configuration'])
        settings={'hostname':original['hostname'],'port':original['https']['port'],
                  'password':original['harbor_admin_password'],'ca':'/etc/kubernetes/ssl/ca.pem',
                  'docker':'/usr/local/bin/docker'}
        self.node('settings',payload=settings)
        write(self.evidence/'created-business.json',self.node('create'))
        self.record_node('source')
        print('HARBOR_SOURCE_213_BUSINESS_PASS',flush=True)

    def migration_phase(self):
        self.select()
        self.maintenance('backup')
        self.maintenance('upgrade')
        self.record_node('migrated')
        self.maintenance('upgrade')
        self.record_node('idempotent')
        print('HARBOR_AMD64_MIGRATION_BUSINESS_PASS',flush=True)

    def restore_phase(self):
        self.select()
        self.maintenance('restore')
        self.record_node('restored')
        print('HARBOR_AMD64_RESTORE_BUSINESS_PASS',flush=True)

    def new_install_phase(self):
        if (self.evidence/'target-config.json').exists():
            self.repair_new_install()
            return
        self.host_operation('marked')
        self.bootstrap(TARGET_HOST)
        self.configure(target=True)
        for stage in ['01','03','11']:
            self.cli(self.target,self.target,'setup',self.cluster,stage)
        self.finish_new_install()

    def finish_new_install(self):
        original=yaml.safe_load(self.node('configuration',host=TARGET_HOST)['configuration'])
        self.node('settings',host=TARGET_HOST,payload={'hostname':original['hostname'],
            'port':original['https']['port'],'password':original['harbor_admin_password'],
            'ca':'/etc/kubernetes/ssl/ca.pem','docker':'/usr/local/bin/docker'})
        write(self.evidence/'target-created-business.json',self.node('create',host=TARGET_HOST))
        self.record_node('target-installed',host=TARGET_HOST)
        self.cli(self.target,self.target,'setup',self.cluster,'11')
        self.record_node('target-idempotent',host=TARGET_HOST)
        self.preserve('harbor-new-install',1,host=TARGET_HOST)
        print('HARBOR_AMD64_CLEAN_INSTALL_IDEMPOTENCE_PASS',flush=True)

    def repair_new_install_boundary(self):
        """Reuse only the proven prepare/runtime preceding a missing-tar failure."""
        if json.loads(self.owner.read_text()) != self.plan:
            raise ValueError('the original Harbor session must retain ownership')
        from upgrade.lab.next_session import LOCKS, LOCK_DIRECTORY
        for name in LOCKS.values():
            with (LOCK_DIRECTORY/name).open('r') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:raise ValueError('the original supervisor must retain all shared leases')
        for phase in ['harbor-source','harbor-migration','harbor-restore']:
            if json.loads((self.evidence/(phase+'.status.json')).read_text())['rc'] != 0:
                raise ValueError('accepted source/migration/restore prerequisite missing')
        failed=json.loads((self.evidence/'harbor-new-install.status.json').read_text())
        if failed['rc'] != 1 or failed['attempt'] != 1:
            raise ValueError('only the first pre-extraction failure can use this repair')
        failures=[]
        for line in (self.evidence/failed['log']).read_text().splitlines():
            if line.startswith('fatal:') and '=> ' in line:
                failures.append(json.loads(line.split('=> ',1)[1]))
        if (len(failures)!=1 or failures[0].get('rc')!=127 or
                failures[0].get('stderr')!='/bin/sh: line 1: tar: command not found' or
                failures[0].get('cmd')!='cd /var/data && tar zxf harbor-offline-installer-v2.16.0.tgz'):
            raise ValueError('repair does not match the proven missing archive tool')
        self.select(target=True)
        config=json.loads((self.evidence/'target-config.json').read_text())
        path=self.target/'clusters'/self.target_cluster/'config.yml'
        if config != {'path':str(path),'sha256':sha(path)}:
            raise ValueError('target configuration changed after its original preparation')
        self.host_operation('marked')
        if self.node('inventory',host=TARGET_HOST)['containers']:
            raise ValueError('the failed pre-extraction target must have no Harbor containers')
        for name in ['target-created-business.json','target-installed-business.json','target-idempotent-business.json']:
            if (self.evidence/name).exists():
                raise ValueError('repair must not replace an existing business identity')

    def repair_new_install(self):
        self.repair_new_install_boundary()
        # Actual setup11 consumes the same original configuration and CA.
        # setup01/03 and the already-accepted source chain are not repeated.
        self.cli(self.target,self.target,'setup',self.cluster,'11')
        self.finish_new_install()
        self.record_node('repair-source-preserved')
        self.host_operation('marked')
        print('HARBOR_NEW_INSTALL_REPAIR_SCOPE_VERIFY_PASS lease=held',flush=True)

    def preserve(self, phase, attempt, *, host=SOURCE_HOST):
        suffix=('.target' if host==TARGET_HOST else '')+'.node-state'
        destination=getattr(self,'archive_directory',self.evidence)/(phase+'.attempt-'+str(attempt)+suffix)
        destination.mkdir(mode=0o700)
        # A lexical last phase ("source") is not the newest complete archive.
        # Reuse all completed snapshots from this host; partial transfers and
        # the other host are never recovery inputs. Rsync checks file content
        # before linking, while each historical snapshot remains unchanged.
        previous=[]
        for receipt in sorted(self.evidence.glob('*.node-state.manifest.json')):
            record=json.loads(receipt.read_text())
            path=Path(record['directory'])
            if record.get('host')==host and path.is_dir() and path!=destination:
                previous.append(path)
        link=['--link-dest='+str(p) for p in previous]
        self.command(['rsync','-a','--checksum','--safe-links','--chmod=D700,F600',*link,
            'root@'+host+':'+NODE_STATE+'/',str(destination)+'/'])
        write(self.evidence/(destination.name+'.manifest.json'),
              {'directory':str(destination),'host':host,'files':{
                  str(p.relative_to(destination)):sha(p) for p in destination.rglob('*') if p.is_file()}})
        return destination

    def clean_phase(self, target=False):
        if not target and (self.evidence/'target-config.json').exists():
            self.clean_phase(target=True)
        host=TARGET_HOST if target else SOURCE_HOST
        prefix='target-' if target else ''
        self.select(target)
        self.host_operation('marked')
        if not (self.evidence/(prefix+'harbor-data-clean.json')).exists():
            self.node('stop',host=host)
            write(self.evidence/(prefix+'harbor-data-clean.json'),self.node('clean-data',host=host))
        checkpoint=self.evidence/(prefix+'harbor-recovery-archive.json')
        if not checkpoint.exists():
            attempt=1+len(list(self.evidence.glob('harbor-clean.attempt-*.node-state')))
            archive=self.preserve('harbor-clean',attempt,host=host)
            archived={str(p.relative_to(archive)):sha(p) for p in archive.rglob('*') if p.is_file()}
            write(checkpoint,{'directory':str(archive),'files':archived})
        archived=json.loads(checkpoint.read_text())
        if any(sha(Path(archived['directory'])/p)!=digest for p,digest in archived['files'].items()):
            raise ValueError('controller recovery archive changed; refuse removal of source backup')
        if not (self.evidence/(prefix+'harbor-runtime-clean.json')).exists():
            self.node('runtime-clean-admission',host=host)
            self.command(['bash',ROOT/'tests/helpers/lab-wipe-nodes.sh','--rocky-only',
                          *([host] if target else self.plan['leased_hosts'])])
            write(self.evidence/(prefix+'harbor-runtime-clean.json'),{'marker':'HARBOR_RUNTIME_CLEAN_PASS'})
        if not (self.evidence/(prefix+'harbor-node-clean.json')).exists():
            write(self.evidence/(prefix+'harbor-node-clean.json'),self.node('clean-state',host=host,payload=archived['files']))
        if not target:self.host_operation('clean')
        print('HARBOR_FOCUSED_CLEAN_VERIFY_PASS',flush=True)

    def run(self):
        for signum in [signal.SIGINT,signal.SIGTERM]:
            signal.signal(signum,lambda signum,frame:print(
                'HARBOR_SIGNAL lease=retained use=explicit-scoped-cleanup',flush=True))
        self.owner_check()
        operations=dict(zip(PHASES,[self.source_phase,self.migration_phase,self.restore_phase,self.new_install_phase,self.clean_phase]))
        operations['harbor-portable']=self.portable_phase
        phases=self.plan['phases']
        for phase in phases:
            operation=operations[phase]
            attempt=1
            while True:
                log=self.evidence/(phase+'.attempt-'+str(attempt)+'.log')
                started=datetime.now(timezone.utc).isoformat()
                try:
                    with phase_log(log):
                        admit_static(MATRIX_PATH,required=[f'UPG-{n:02}' for n in range(1,5)])
                        operation()
                        if phase!='harbor-clean':self.preserve(phase,attempt)
                        self.host_operation('marked')
                        print('HARBOR_PHASE_SCOPE_VERIFY_PASS phase='+phase+' lease=held',flush=True)
                except Exception as error:
                    failure={'run_id':self.run_id,'phase':phase,'attempt':attempt,'rc':1,
                             'reason':str(error),'started':started,'log':log.name,'lease':'held'}
                    write(self.evidence/(phase+'.status.json'),failure)
                    write(self.evidence/(phase+'.failure-'+str(attempt)+'.json'),failure)
                    print('HARBOR_MODULE_STOP phase='+phase+' lease=held',flush=True)
                    action=self.wait(phase,failed=True)
                    if action=='retry' and self.mode!='final':attempt+=1;continue
                    self.guarded_shutdown(phase)
                    raise ValueError('Harbor failed; original failure preserved and scoped cleanup completed')
                write(self.evidence/(phase+'.status.json'),{'run_id':self.run_id,'phase':phase,
                    'attempt':attempt,'rc':0,'started':started,'finished':datetime.now(timezone.utc).isoformat(),
                    'log':log.name})
                for suffix in ['exit','finalized']:
                    path=self.evidence/(phase+'.attempt-'+str(attempt)+'.'+suffix)
                    with path.open('x') as stream:
                        stream.write('0\n');stream.flush();os.fsync(stream.fileno())
                break
            if self.mode!='final' and phase!=phases[-1] and self.wait(phase)=='cleanup':
                self.guarded_shutdown(phase)
                raise ValueError('Harbor qualification ended early; scoped cleanup completed')
        while True:
            try:
                self.host_operation('release')
                break
            except Exception:
                print('HARBOR_RELEASE_STOP lease=held',flush=True)
                self.wait('harbor-clean',failed=True)
        self.owner.unlink()
        print('HARBOR_AMD64_SOURCE_MIGRATION_RESTORE_QUALIFICATION_PASS',flush=True)
        if self.mode=='final':print('HARBOR_AFFECTED_FINAL_ACCEPTANCE_PASS',flush=True)
        print('LAB_CLEAN_VERIFY_PASS',flush=True)

    def shutdown(self):
        self.clean_phase()
        self.host_operation('release')
        self.owner.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['plan','run','repair-new-install','repair-cleanup','repair-portable','repair-portable-backup','supply-paused-packages','resume-supplied-supervisor'])
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--mode',choices=['qualify','portable','final'],default='qualify')
    parser.add_argument('--parent-run')
    parser.add_argument('--supervisor-pid',type=int)
    args=parser.parse_args()
    if args.operation=='plan':print(json.dumps(plan(args.run_id,args.mode),ensure_ascii=False,indent=2))
    elif args.operation=='run':HarborSession(args.run_id,args.mode).run()
    elif args.operation=='supply-paused-packages':
        supply_paused_packages(HarborSession(args.run_id,'final'),args.parent_run,args.supervisor_pid)
    elif args.operation=='resume-supplied-supervisor':
        resume_supplied_supervisor(HarborSession(args.run_id,'final'),args.parent_run,args.supervisor_pid)
    elif args.operation=='repair-new-install':HarborSession(args.run_id).repair_new_install()
    elif args.operation=='repair-portable-backup':HarborSession(args.run_id,'portable').portable_phase(resume_backup=True)
    elif args.operation=='repair-portable':HarborSession(args.run_id,'portable').portable_phase(resume_export=True)
    else:
        session=HarborSession(args.run_id)
        if json.loads(session.owner.read_text()) != session.plan:
            raise ValueError('original Harbor cleanup owner required')
        for suffix in ['exit','finalized']:
            if (session.evidence/('harbor-new-install-repair.attempt-1.'+suffix)).read_text().strip()!='0':
                raise ValueError('new-install repair must finish before cleanup')
        session.clean_phase()
        print('HARBOR_CLEANUP_REPAIR_PASS lease=held',flush=True)


if __name__=='__main__':
    main()
