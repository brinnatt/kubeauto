"""Harbor-only native ARM supplement; no Kubernetes install, build or release.

Run on the fixed x86 control host with the existing kubeauto Python environment.
Reuse the Harbor command supervision, isolated kubecli, node ownership, business
ledger and recovery archive contracts. Only the ARM fixture boundary differs.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

import yaml

from upgrade.lab.harbor_session import HarborSession, NODE_STATE
from upgrade.lab.next_session import ROOT, STATE, write, phase_log
from upgrade.product.launcher import admit_static
from upgrade.product.hop import MATRIX_PATH


HOST = '192.168.37.101'
CONTROL = '192.168.47.130'
PHASES = ['harbor-source', 'harbor-migration', 'harbor-restore',
          'harbor-new-install', 'harbor-clean']
PACKAGES = {
    'v2.13.0': '4c2c6b2055e79c364166529349187d5edc78055941ffec3d5b4e70269f778320',
    'v2.14.4': '524ab0439cdb421f3226c689c5d5799f9e4dda6dd3a26dbdf0cc2311690e0626',
    'v2.16.0': '6525af9c26cc24e48ecf37d3f7d9d139895b39a037a3de697f995171ce65524a',
}
FIRST_TASK = 'Validate the path consumed by the official Harbor prepare script'
LEASE = Path('/var/lock/kubeauto-harbor-arm-192.168.37.101.lock')
CONTROL_BINARIES = {'kube-bin':['kubelet','kubectl','kube-apiserver'],
                    'extra-bin':['etcdctl','etcd','cfssl','cfssljson']}


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def expected_images():
    result = {}
    for name in ['harbor-v2.13.0-arm64-image-content.json',
                 'harbor-v2.14.4-iabs-arm64-image-content.json',
                 'harbor-v2.16.0-arm64-image-content.json']:
        record = json.loads((MATRIX_PATH.parent/'evidence/raw/harbor-content'/name).read_text())
        for image in record['images']:
            identity = image.get('id') or 'sha256:' + image['config_sha256']
            for tag in image['tags']:
                if tag in result and result[tag] != identity:
                    raise ValueError('conflicting qualified image identity')
                result[tag] = identity
    return result


def verify_image_cache(existing, expected):
    for tag, identity in existing.items():
        if tag in expected and identity != expected[tag]:
            raise ValueError('foreign image content would be overwritten: '+tag)


def plan(run_id, mode):
    if not re.fullmatch(r'v1366-harbor-arm-[a-z0-9-]{1,28}', run_id):
        raise ValueError('fixed ARM Harbor run identity required')
    if mode not in ['qualify', 'final']:
        raise ValueError('fixed ARM Harbor mode required')
    required = range(1, 15) if mode == 'final' else range(1, 5)
    admit_static(MATRIX_PATH, required=[f'UPG-{n:02}' for n in required])
    scope = yaml.safe_load((MATRIX_PATH.parent/'acceptance-plan.yaml').read_text()).get('harbor_native_arm64_supplement', {})
    if (scope.get('enabled') is not True or scope.get('active_hosts') != [HOST] or
            scope.get('control') != CONTROL or scope.get('phases') != PHASES or
            scope.get('runner') != 'upgrade/lab/harbor_arm_session.py'):
        raise ValueError('exact native ARM Harbor scope must be frozen')
    return {'run_id': run_id, 'mode': mode, 'active_hosts': [HOST],
            'control': CONTROL, 'phases': PHASES, 'packages': PACKAGES,
            'builds': [], 'kubernetes_mutation': 'none',
            'entry': 'config.yml + kubecli setup11 (Harbor role on verified runtime); upgrade --harbor',
            'cleanup': 'owned Harbor data, TLS, trust, fixtures and archives; restore inactive Docker; preserve shared image cache'}


class ArmHarborSession(HarborSession):
    """One ARM node: migrate/restore first, then clean target installation."""
    def __init__(self, run_id, mode='qualify'):
        self.plan = plan(run_id, mode)
        self.run_id, self.mode = run_id, mode
        self.state = STATE
        self.source = self.target = ROOT
        self.base = ROOT
        self.cluster = 'harbor-arm-' + hashlib.sha256(run_id.encode()).hexdigest()[:8]
        self.evidence = STATE/'runs'/run_id
        self.owner = STATE/'owner.json'
        self.archive_directory = STATE/'recovery'/run_id

    def remote(self, host, arguments, *, data=None):
        if host != HOST:
            raise ValueError('host escaped native ARM Harbor scope')
        return self.command(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                             'root@'+host, shlex.join([str(a) for a in arguments])],
                            capture=True, data=data)

    def node(self, action, *, host=HOST, payload=None):
        return json.loads(self.remote(host, ['python3', '-B', NODE_STATE+'/harbor_node.py',
                          action, '--run-id', self.run_id],
                          data=json.dumps(payload) if payload is not None else None))

    def owner_check(self):
        if os.geteuid() != 0 or CONTROL not in subprocess.check_output(['hostname', '-I'], text=True).split():
            raise ValueError('fixed root control host required')
        if self.state.is_symlink() or self.state.stat().st_uid != 0 or self.state.stat().st_mode & 0o777 != 0o700:
            raise ValueError('private root-owned staging directory required')
        if self.owner.exists() or self.owner.is_symlink() or self.evidence.exists():
            raise ValueError('prior owner or run must be inspected, never adopted')
        manifest = json.loads((self.state/'arm-inputs.json').read_text())
        if manifest.get('run_id') != self.run_id or manifest.get('host') != HOST or not manifest.get('files'):
            raise ValueError('fixed ARM staged manifest identity required')
        for name, checksum in manifest['files'].items():
            path = self.state/name
            if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink() or sha(path) != checksum:
                raise ValueError('staged ARM Harbor input changed: '+name)
        for version, checksum in PACKAGES.items():
            if sha(ROOT/'down'/('harbor-offline-installer-'+version+'.tgz')) != checksum:
                raise ValueError('fixed ARM offline package missing or corrupt')
        self.evidence.mkdir(mode=0o700, parents=True)
        self.archive_directory.mkdir(mode=0o700, parents=True)
        write(self.owner, self.plan)
        write(self.evidence/'plan.json', self.plan)

    def admission(self):
        # Read-only before claiming the node. Existing Docker installation and
        # its cache are not ours to erase; a clean stopped runtime is required.
        program = '''import os,platform,json,subprocess
from pathlib import Path
assert os.geteuid()==0 and platform.machine()=='aarch64'
for name in ['docker','docker.socket','containerd','kubelet','etcd','cri-dockerd']:
 value=subprocess.check_output(['systemctl','show',name,'--property=ActiveState','--value'],text=True).strip()
 assert value in ['inactive','unknown'],name
for path in ['/var/data','/var/log/harbor','/etc/kubernetes','/var/tmp/kubeauto-v1366-node-owner','/var/tmp/kubeauto-harbor-v1366','/usr/local/bin/docker','/usr/local/bin/docker-compose','/etc/docker/certs.d/harbor.upgrade.invalid:8443']:
 assert not Path(path).exists() and not Path(path).is_symlink(),path
assert not list(Path('/var/lib/docker/containers').glob('*'))
assert Path('/usr/libexec/docker/cli-plugins/docker-compose').is_file()
assert 'harbor.upgrade.invalid' not in Path('/etc/hosts').read_text()
assert not Path('/etc/pki/ca-trust/source/anchors/kubeauto-harbor-arm.pem').exists()
daemon=Path('/etc/docker/daemon.json')
if daemon.exists():
 assert not daemon.is_symlink()
 assert json.loads(daemon.read_text()).get('data-root','/var/lib/docker')=='/var/lib/docker'
import time
print(json.dumps({'architecture':platform.machine(),'page_size':os.sysconf('SC_PAGE_SIZE'),'docker':'inactive','foreign_containers':0,'epoch':time.time()}))
'''
        inventory = json.loads(self.remote(HOST, ['python3', '-B', '-'], data=program))
        if abs(time.time()-inventory['epoch']) > 120:
            raise ValueError('ARM snapshot clock differs; refuse certificate creation')
        write(self.evidence/'node-admission.json', inventory)
        marker = 'from pathlib import Path; import os; p=Path("/var/tmp/kubeauto-v1366-node-owner"); f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600); os.write(f,'+repr(self.run_id.encode())+'); os.close(f)'
        self.remote(HOST, ['python3', '-B', '-c', marker])
        self.bootstrap(HOST)

    def fixture(self):
        self.admission()
        write(self.evidence/'runtime-config-isolation.json',self.node('arm-runtime-fixture'))
        # Existing distribution runtime satisfies Harbor's >20.10 / >2.3
        # contract. Do not install Kubernetes, CRI or upgrade this OS runtime.
        self.remote(HOST, ['bash', '-se'], data='''
umask 077
systemctl start docker
test -z "$(docker ps -aq)"
ln -s /usr/bin/docker /usr/local/bin/docker
install -m 0755 /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose
''')
        image_rows = self.remote(HOST, ['/usr/bin/docker', 'image', 'ls', '--no-trunc',
                                      '--format', '{{json .}}'])
        existing = {}
        for line in image_rows.splitlines():
            row = json.loads(line)
            existing[row['Repository']+':'+row['Tag']] = row['ID']
        verify_image_cache(existing, expected_images())
        write(self.evidence/'image-cache-admission.json', {
            'checked_tags': len(expected_images()), 'conflicts': 0,
            'policy': 'preserve cache; refuse different bytes under package tags'})
        self.cli(ROOT, ROOT, 'new', self.cluster)
        directory = ROOT/'clusters'/self.cluster
        hosts = '[etcd]\n[kube_master]\n[kube_node]\n[harbor]\n'+HOST+' NEW_INSTALL=true\n[ex_lb]\n[chrony]\n[all:vars]\n'
        hosts += 'ansible_user=root\nansible_python_interpreter=/usr/bin/python3\nbin_dir=/usr/local/bin\nbase_dir='+str(ROOT)+'\ncluster_dir='+str(directory)+'\nca_dir=/etc/kubernetes/ssl\nCONTAINER_RUNTIME=docker\n'
        (directory/'hosts').write_text(hosts)
        config = yaml.safe_load((directory/'config.yml').read_text())
        config.update(HARBOR_DOMAIN='harbor.upgrade.invalid', HARBOR_PATH='/var/data',
                      HARBOR_TLS_PORT=8443, HARBOR_WITH_TRIVY=False, HARBOR_SELF_SIGNED_CERT=False,
                      INSECURE_REG=[], ENABLE_MIRROR_REGISTRY=False,
                      KUBE_RESERVED_ENABLED='no', SYS_RESERVED_ENABLED='no')
        (directory/'config.yml').write_text(yaml.safe_dump(config, sort_keys=False))
        (directory/'config.yml').chmod(0o600)
        self.remote(HOST, ['bash', '-se'], data='''
umask 077
mkdir /etc/kubernetes
mkdir /etc/kubernetes/ssl
openssl req -x509 -newkey rsa:2048 -nodes -days 3 -subj /CN=Harbor-ARM-fixture \
  -keyout /etc/kubernetes/ssl/ca-key.pem -out /etc/kubernetes/ssl/ca.pem 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj /CN=harbor.upgrade.invalid \
  -keyout /etc/kubernetes/ssl/harbor-key.pem -out /etc/kubernetes/ssl/harbor.csr 2>/dev/null
printf 'subjectAltName=DNS:harbor.upgrade.invalid,IP:192.168.37.101\nextendedKeyUsage=serverAuth\n' > /etc/kubernetes/ssl/ext.cnf
openssl x509 -req -days 3 -in /etc/kubernetes/ssl/harbor.csr -CA /etc/kubernetes/ssl/ca.pem \
  -CAkey /etc/kubernetes/ssl/ca-key.pem -CAcreateserial -extfile /etc/kubernetes/ssl/ext.cnf \
  -out /etc/kubernetes/ssl/harbor.pem 2>/dev/null
test ! -e /etc/pki/ca-trust/source/anchors/kubeauto-harbor-arm.pem
cp /etc/kubernetes/ssl/ca.pem /etc/pki/ca-trust/source/anchors/kubeauto-harbor-arm.pem
update-ca-trust
printf '\n192.168.37.101 harbor.upgrade.invalid # kubeauto-harbor-arm-fixture\n' >> /etc/hosts
''')
        for name in ['harbor.pem', 'harbor-key.pem']:
            data = self.remote(HOST, ['cat', '/etc/kubernetes/ssl/'+name])
            path = ROOT/'down'/name
            with path.open('x') as stream:
                stream.write(data)
            path.chmod(0o600)
        self.command(['scp', '-p', 'root@'+HOST+':/usr/libexec/docker/cli-plugins/docker-compose',
                      str(ROOT/'extra-bin/docker-compose')])
        for version in ['v2.14.4', 'v2.16.0']:
            self.command(['scp', '-p', str(ROOT/'down'/('harbor-offline-installer-'+version+'.tgz')),
                          'root@'+HOST+':'+NODE_STATE+'/'])

    def install(self, version):
        path = ROOT/'clusters'/self.cluster/'config.yml'
        value = yaml.safe_load(path.read_text()); value['HARBOR_VER'] = version
        path.write_text(yaml.safe_dump(value, sort_keys=False)); path.chmod(0o600)
        # The fixture has already proved a healthy native Docker/Compose and
        # TLS baseline. Enter the real Harbor role through the public CLI,
        # without running unrelated host preparation or CRI installation.
        self.cli(ROOT, ROOT, 'setup', self.cluster, '11', '--start-at-task', shlex.quote(FIRST_TASK))

    def settings(self):
        value = yaml.safe_load(self.node('configuration')['configuration'])
        self.node('settings', payload={'hostname':value['hostname'], 'port':value['https']['port'],
                  'password':value['harbor_admin_password'], 'ca':'/etc/kubernetes/ssl/ca.pem',
                  'docker':'/usr/local/bin/docker'})

    def verify(self, label):
        inventory = self.node('inventory')
        version = 'v2.13.0-aarch64' if label in ['source', 'restored'] else 'v2.16.0'
        qualified = expected_images()
        if inventory['architecture'] != 'aarch64' or not inventory['containers']:
            raise ValueError('native ARM containers required for business verification')
        for row in inventory['containers']:
            image = row['Config']['Image']
            if not image.endswith(':'+version) or qualified.get(image) != row['Image']:
                raise ValueError('running Harbor image differs from qualified release: '+image)
        # Docker inspect includes secret environment/configuration values.
        # Retain only public runtime identities in collected evidence.
        write(self.evidence/(label+'-inventory.json'), {'architecture':inventory['architecture'],
              'containers':[{'id':r['Id'],'image_id':r['Image'],'image':r['Config']['Image'],
                             'state':r['State']} for r in inventory['containers']]})
        write(self.evidence/(label+'-business.json'), self.node('verify'))

    def maintenance(self, operation):
        args = ['upgrade', self.cluster, '--harbor', operation, '--harbor-backup-dir', NODE_STATE+'/backup-original']
        if operation == 'backup':
            args += ['--harbor-ca-file', '/etc/kubernetes/ssl/ca.pem']
        if operation == 'upgrade':
            args += ['--harbor-bridge-package', NODE_STATE+'/harbor-offline-installer-v2.14.4.tgz',
                     '--harbor-target-package', NODE_STATE+'/harbor-offline-installer-v2.16.0.tgz']
        self.cli(ROOT, ROOT, *args)

    def source_phase(self):
        self.fixture(); self.install('v2.13.0'); self.settings()
        write(self.evidence/'source-created.json', self.node('create'))
        self.verify('source')
        print('HARBOR_ARM_SOURCE_BUSINESS_PASS', flush=True)

    def migration_phase(self):
        self.maintenance('backup'); self.maintenance('upgrade'); self.verify('migrated')
        self.maintenance('upgrade'); self.verify('migration-idempotent')
        print('HARBOR_ARM_NATIVE_MIGRATION_PASS', flush=True)

    def restore_phase(self):
        self.maintenance('restore'); self.verify('restored')
        print('HARBOR_ARM_ORIGINAL_RESTORE_PASS', flush=True)

    def new_install_phase(self):
        self.node('stop'); self.node('clean-data'); self.node('retire-fixture')
        # Reuse the exact runner-owned TLS identity across the two independent
        # installs; retirement has kept its original bytes in private state.
        self.remote(HOST, ['bash', '-se'], data='for name in ca.pem harbor.pem harbor-key.pem; do\n'
                    'test ! -e /etc/kubernetes/ssl/$name\n'
                    'cp '+NODE_STATE+'/previous-target-fixture/$name /etc/kubernetes/ssl/$name\ndone\n')
        self.install('v2.16.0'); self.settings()
        write(self.evidence/'new-created.json', self.node('create'))
        self.verify('new-install'); self.install('v2.16.0'); self.verify('new-idempotent')
        print('HARBOR_ARM_CLEAN_INSTALL_IDEMPOTENCE_PASS', flush=True)

    def clean_phase(self):
        restored = self.evidence/'runtime-config-restoration.json'
        if not restored.exists():
            self.node('stop'); self.node('clean-data')
            self.remote(HOST, ['systemctl', 'stop', 'docker.socket', 'docker', 'containerd'])
            write(restored,self.node('arm-runtime-restore'))
        else:
            # A failed archive transfer must not start the restored runtime or
            # restore its configuration a second time. Prove the saved boundary.
            self.remote(HOST, ['python3','-B','-'], data=
                'import json,hashlib,subprocess\nfrom pathlib import Path\n'
                's=Path('+repr(NODE_STATE)+'); r=json.loads((s/"daemon-fixture.json").read_text())\n'
                'p=Path("/etc/docker/daemon.json")\nassert not p.is_symlink()\n'
                'if r["exists"]:\n'
                ' raw=(s/"original-daemon.json").read_bytes()\n'
                ' assert hashlib.sha256(raw).hexdigest()==r["sha256"] and p.read_bytes()==raw\n'
                ' assert (p.stat().st_uid,p.stat().st_gid,p.stat().st_mode&0o777)==(r["uid"],r["gid"],r["mode"])\n'
                'else: assert not p.exists()\n'
                'assert not Path("/var/data").exists() and not Path("/var/log/harbor").exists()\n'
                'assert not list(Path("/var/lib/docker/containers").glob("*"))\n'
                'for unit in ["docker","docker.socket","containerd"]:\n'
                ' assert subprocess.check_output(["systemctl","show",unit,"--property=ActiveState","--value"],text=True).strip()=="inactive"\n')
        destination = self.archive_directory/Path(NODE_STATE).name
        self.remote(HOST, ['python3','-B','-'], data=
            'from pathlib import Path\np=Path('+repr(NODE_STATE)+')\n'
            'assert not p.is_symlink()\n'
            'assert all(not x.is_symlink() and (x.is_file() or x.is_dir()) for x in p.rglob("*"))\n')
        # Copy the named directory to its parent. Legacy SCP rejects a remote
        # basename of "."; this form also works when OpenSSH uses SFTP.
        self.command(['scp', '-rp', 'root@'+HOST+':'+NODE_STATE, str(self.archive_directory)+'/'])
        for path in destination.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        files = {str(p.relative_to(destination)):sha(p) for p in destination.rglob('*') if p.is_file()}
        if not files:
            raise ValueError('complete private recovery archive required before cleanup')
        write(self.evidence/'recovery-archive.json', {'directory':str(destination),'files':files})
        self.node('clean-state', payload=files)
        program = '''from pathlib import Path
import subprocess
import hashlib
trust=Path('/etc/docker/certs.d/harbor.upgrade.invalid:8443')
ca=Path('/etc/kubernetes/ssl/ca.pem').read_bytes()
if TRUST_REQUIRED:
 assert not trust.is_symlink() and {p.name for p in trust.iterdir()}=={'ca.crt'}
 assert not (trust/'ca.crt').is_symlink() and (trust/'ca.crt').read_bytes()==ca
 (trust/'ca.crt').unlink();trust.rmdir()
else:
 assert not trust.exists() and not trust.is_symlink()
anchor=Path('/etc/pki/ca-trust/source/anchors/kubeauto-harbor-arm.pem')
assert not anchor.is_symlink() and anchor.read_bytes()==ca
assert Path('/usr/local/bin/docker').is_symlink() and Path('/usr/local/bin/docker').readlink()==Path('/usr/bin/docker')
assert Path('/usr/local/bin/docker-compose').read_bytes()==Path('/usr/libexec/docker/cli-plugins/docker-compose').read_bytes()
for name in ['/usr/local/bin/docker-compose','/usr/local/bin/docker']:
 Path(name).unlink()
Path('/etc/pki/ca-trust/source/anchors/kubeauto-harbor-arm.pem').unlink()
subprocess.run(['update-ca-trust'],check=True)
p=Path('/etc/hosts'); lines=p.read_text().splitlines(True)
p.write_text(''.join(x for x in lines if x.strip()!='192.168.37.101 harbor.upgrade.invalid # kubeauto-harbor-arm-fixture'))
import shutil
assert {p.name for p in Path('/etc/kubernetes').iterdir()}=={'ssl'}
assert {p.name for p in Path('/etc/kubernetes/ssl').iterdir()}=={'ca-key.pem','ca.pem','harbor-key.pem','harbor.csr','ext.cnf','harbor.pem','ca.srl'}
shutil.rmtree('/etc/kubernetes')
p=Path('/var/tmp/kubeauto-v1366-node-owner')
assert p.read_text()==RUN_ID
for path in ['/var/data','/var/log/harbor','/etc/kubernetes','/var/tmp/kubeauto-harbor-v1366','/usr/local/bin/docker-compose','/usr/local/bin/docker']:
 assert not Path(path).exists() and not Path(path).is_symlink(),path
assert not trust.exists() and not anchor.exists()
assert 'harbor.upgrade.invalid' not in Path('/etc/hosts').read_text()
for unit in ['docker','docker.socket','containerd']:
 assert subprocess.check_output(['systemctl','show',unit,'--property=ActiveState','--value'],text=True).strip()=='inactive',unit
p.unlink()
print('HARBOR_ARM_SCOPED_CLEAN_VERIFY_PASS')
'''
        output = self.remote(HOST, ['python3', '-B', '-'], data='RUN_ID='+repr(self.run_id)+
                             '\nTRUST_REQUIRED='+repr('settings.json' in files)+'\n'+program)
        if 'HARBOR_ARM_SCOPED_CLEAN_VERIFY_PASS' not in output:
            raise ValueError('native scoped cleanup incomplete')
        print(output.strip(), flush=True)

    def cleanup_uninstalled(self):
        """Recover only a failed source fixture before any Harbor installation."""
        with LEASE.open('a') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if json.loads(self.owner.read_text()) != self.plan:
                raise ValueError('original failed fixture owner required')
            failure=json.loads((self.evidence/'harbor-source.status.json').read_text())
            if failure['rc']!=1 or (self.evidence/'source-created.json').exists() or (self.evidence/'harbor-migration.status.json').exists():
                raise ValueError('only an uninstalled source fixture can use this cleanup')
            self.remote(HOST,['python3','-B','-'],data=
                'import sys\nfrom pathlib import Path\nsys.path.insert(0,'+repr(NODE_STATE)+')\n'
                'from harbor_node import owner\nowner('+repr(self.run_id)+')\n'
                's=Path('+repr(NODE_STATE)+')\n'
                'assert not Path("/var/data").exists() and not Path("/var/log/harbor").exists()\n'
                'assert not list(Path("/var/lib/docker/containers").glob("*"))\n'
                'assert not (s/"ledger.json").exists() and not list(s.glob("backup-*"))\n')
            write(self.evidence/'fixture-cleanup-helper.json',{'sha256':sha(Path(__file__)),
                  'scope':'uninstalled source fixture only; original parent failure remains unchanged'})
            # These are immutable test inputs, not a recovery point. Keep the
            # verified controller copies; do not retransmit them as failed data.
            for version,checksum in PACKAGES.items():
                if sha(ROOT/'down'/('harbor-offline-installer-'+version+'.tgz'))!=checksum:
                    raise ValueError('verified controller package required before removing duplicate fixture inputs')
            self.remote(HOST,['python3','-B','-'],data=
                'import hashlib\nfrom pathlib import Path\ns=Path('+repr(NODE_STATE)+')\n'
                'for version,checksum in '+repr(PACKAGES)+'.items():\n'
                ' p=s/("harbor-offline-installer-"+version+".tgz")\n'
                ' if not p.exists(): continue\n'
                ' assert not p.is_symlink() and p.is_file()\n'
                ' h=hashlib.sha256()\n'
                ' with p.open("rb") as f:\n'
                '  for block in iter(lambda:f.read(1048576),b""):h.update(block)\n'
                ' assert h.hexdigest()==checksum\n p.unlink()\n')
            self.clean_phase()
            print('HARBOR_ARM_UNINSTALLED_FIXTURE_CLEANED',flush=True)

    def run(self):
        with LEASE.open('a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owner_check()
            for phase, operation in zip(PHASES, [self.source_phase, self.migration_phase,
                        self.restore_phase, self.new_install_phase, self.clean_phase]):
                log = self.evidence/(phase+'.log')
                try:
                    with phase_log(log):
                        operation()
                except BaseException as error:
                    write(self.evidence/(phase+'.status.json'), {'run_id':self.run_id,'rc':1,'reason':str(error)})
                    for suffix in ['exit','finalized']:
                        with (self.evidence/(phase+'.'+suffix)).open('x') as stream:
                            stream.write('1\n'); stream.flush(); os.fsync(stream.fileno())
                    print('HARBOR_ARM_STOP phase='+phase+' owner=retained', flush=True)
                    raise
                write(self.evidence/(phase+'.status.json'), {'run_id':self.run_id,'rc':0})
                for suffix in ['exit','finalized']:
                    with (self.evidence/(phase+'.'+suffix)).open('x') as stream:
                        stream.write('0\n'); stream.flush(); os.fsync(stream.fileno())
            self.owner.unlink()
            print('HARBOR_ARM_AFFECTED_ACCEPTANCE_PASS', flush=True)
            if self.mode == 'final':
                print('UPGRADE_NEXT_FINAL_PASS scope=harbor-native-arm64-supplement', flush=True)
            print('LAB_CLEAN_VERIFY_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan','run','cleanup-uninstalled'])
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--mode', choices=['qualify','final'], default='qualify')
    args = parser.parse_args()
    if args.operation == 'plan':
        print(json.dumps(plan(args.run_id,args.mode), ensure_ascii=False, indent=2))
    elif args.operation=='cleanup-uninstalled':
        ArmHarborSession(args.run_id,args.mode).cleanup_uninstalled()
    else:
        ArmHarborSession(args.run_id,args.mode).run()


if __name__ == '__main__':
    main()
