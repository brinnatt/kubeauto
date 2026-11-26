#!/usr/bin/env python3
"""Fixed, scoped qualification for the tools' configuration/execution boundary."""
import argparse
import contextlib
import hashlib
import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
REMOTE = 'root@192.168.47.141'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preflight():
    with tempfile.TemporaryDirectory(prefix='tools-execution-contract-') as directory:
        subprocess.run([sys.executable, '-B', '-m', 'unittest',
            'tests.unit.test_tools_execution_boundary_contract',
            'tests.unit.test_tools_api_fixture_contract',
            'tests.unit.test_tools_config_failure_contract', '-q'], cwd=ROOT,
            env=dict(os.environ, KUBEAUTO_LOG_DIR=directory), check=True)
    print('TOOLS_EXECUTION_BOUNDARY_PREFLIGHT_PASS', flush=True)


def remote_command(command, host=REMOTE, **kwargs):
    return subprocess.run([*SSH, host, command], text=True, check=True, timeout=60, **kwargs)


@contextlib.contextmanager
def remote_lease(host=REMOTE):
    command="flock -n /run/lock/kubeauto-core-pool.lock sh -c 'echo TOOLS_BOOTSTRAP_LEASE; cat >/dev/null'"
    process=subprocess.Popen([*SSH,host,command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        if process.stdout.readline().strip()!='TOOLS_BOOTSTRAP_LEASE':
            raise ValueError('TOOLS_BOOTSTRAP_HOST_LEASE_BUSY')
        yield
    finally:
        process.stdin.close()
        process.wait(timeout=20)


def verify_remote_refusal(tool, result):
    output = result.stdout + result.stderr
    expected = ('必须指定 --kafka-home 或配置文件中的 kafka_home' if tool == 'KafkaCli'
                else '清理操作必须指定节点类型: --deploy [fe|be|cn]')
    if result.returncode == 0 or expected not in output or 'SyntaxError' in output or 'ModuleNotFoundError' in output:
        raise ValueError('REMOTE_BOOTSTRAP_HANDOFF_REFUSED '+tool+' '+output[-3000:])


def source_distro_dependency():
    import distro
    if distro.__version__ != '1.9.0':
        raise ValueError('REMOTE_BOOTSTRAP_DISTRO_VERSION_REFUSED')
    # The package __init__.py imports .distro and is not a standalone module.
    return Path(importlib.import_module('distro.distro').__file__)


def remote_bootstrap_owned():
    # This gate owns only an unused temporary directory and two previously
    # absent default log files. It never creates a broker, database or service.
    token = uuid.uuid4().hex[:12]
    base = '/tmp/tools-bootstrap-' + token
    logs = ['/root/logs/kafka_deploy.log', '/root/logs/starrocks_deploy.log']
    probes = 'test ! -e ' + shlex.quote(base)
    probes += '; if command -v kafkacli || command -v starcli; then exit 2; fi'
    probes += '; ' + '; '.join('test ! -e '+shlex.quote(p) for p in logs)
    remote_command('set -e; ' + probes)
    source_files = {'KafkaCli': 'tools/kafka/KafkaCli.py', 'StarCli': 'tools/starrocks/StarCli.py'}
    for tool, relative in source_files.items():
        proof=json.loads((ROOT/'dist'/('kafka-build.json' if tool=='KafkaCli' else 'starcli-build.json')).read_text())
        if proof['inputs'][relative] != sha(ROOT/relative) or proof['artifacts'][tool] != sha(ROOT/'dist'/(tool+'-rocky8')):
            raise ValueError('REMOTE_BOOTSTRAP_BUILD_STALE '+tool)
    remote_command('mkdir -m 700 ' + shlex.quote(base))
    try:
        # StarCli's documented source dependency is supplied beside the uploaded
        # source, never installed into the host Python or copied from another tool.
        distro_source=source_distro_dependency()
        for tool, relative in source_files.items():
            for mode in ('source', 'frozen'):
                work = base+'/'+tool+' '+mode
                remote_command('mkdir -m 700 '+shlex.quote(work))
                if tool == 'StarCli' and mode == 'source':
                    subprocess.run(['scp','-q','-o','BatchMode=yes',str(distro_source),REMOTE+':'+work+'/distro.py'],check=True)
                source = ROOT/relative if mode=='source' else ROOT/'dist'/(tool+'-rocky8')
                with tempfile.TemporaryDirectory(prefix='tools-remote-caller-') as directory:
                    env=dict(os.environ,KUBEAUTO_LOG_DIR=directory,KAFKA_LOG_DIR=directory,STARROCKS_LOG_DIR=directory)
                    command=([sys.executable,'-B',str(source)] if mode=='source' else [str(source)])
                    command += ['--target-host',REMOTE.split('@')[1],'--ssh-user','root',
                                '--ssh-key',str(Path.home()/'.ssh/id_ed25519'),
                                '--remote-workdir',work,'--clean']
                    result=subprocess.run(command,env=env,cwd=directory,text=True,capture_output=True,timeout=120)
                verify_remote_refusal(tool,result)
                filename=('kafka_deploy' if tool=='KafkaCli' else 'starrocks_deploy')+('.py' if mode=='source' else '')
                uploaded=remote_command('sha256sum '+shlex.quote(work+'/'+filename),capture_output=True).stdout.split()[0]
                if uploaded != sha(source):
                    raise ValueError('REMOTE_BOOTSTRAP_UPLOAD_CHECKSUM_REFUSED')
                print('REMOTE_BOOTSTRAP_HANDOFF_PASS tool='+tool+' mode='+mode+' expected_remote_refusal=1 sha256='+uploaded,flush=True)
    finally:
        script='import pathlib,shutil; shutil.rmtree('+repr(base)+'); '
        script+='; '.join('pathlib.Path('+repr(p)+').unlink(missing_ok=True)' for p in logs)
        remote_command('python3 -c '+shlex.quote(script))
        remote_command('test ! -e '+shlex.quote(base)+'; '+'; '.join('test ! -e '+shlex.quote(p) for p in logs))
        print('TOOLS_BOOTSTRAP_SCOPED_CLEAN_PASS host='+REMOTE,flush=True)
    print('TOOLS_REMOTE_BOOTSTRAP_PASS',flush=True)


def remote_bootstrap():
    with remote_lease():
        remote_bootstrap_owned()


def ovpn_config():
    host = 'root@192.168.47.130'
    source = Path('/tmp/kubeauto-easyrsa-3.0.7')
    proof = json.loads((source / 'source.json').read_text())
    if proof['version'] != 'v3.0.7':
        raise ValueError('OVPN_SOURCE_VERSION_REFUSED')
    for name, expected in proof['files'].items():
        if sha(source / name) != expected:
            raise ValueError('OVPN_SOURCE_CHECKSUM_REFUSED ' + name)
    build = json.loads((ROOT / 'dist/ovpn-build.json').read_text())
    product = 'tools/OvpnUserCli.py'
    artifact = ROOT / 'dist/OvpnUserCli-rocky8'
    if build['inputs'][product] != sha(ROOT / product) or build['artifacts']['OvpnUserCli'] != sha(artifact):
        raise ValueError('OVPN_BUILD_STALE')
    base = '/tmp/tools-ovpn-' + uuid.uuid4().hex[:12]
    with remote_lease(host):
        remote_command('openssl version | grep -q "^OpenSSL 1.1.1"', host=host)
        remote_command('test ! -e ' + base + ' && mkdir -m 700 ' + base, host=host)
        try:
            for path, name in ((source, 'source'), (artifact, 'OvpnUserCli'),
                               (ROOT / 'tests/helpers/ovpn-config-fixture.py', 'fixture.py')):
                subprocess.run(['scp', '-q', '-r', '-o', 'BatchMode=yes', str(path), host + ':' + base + '/' + name], check=True)
            remote_command('test "$(sha256sum ' + base + '/OvpnUserCli | cut -d" " -f1)" = ' + sha(artifact), host=host)
            result = subprocess.run([*SSH, host, 'cd ' + base + ' && python3 -B fixture.py'],
                                    text=True, capture_output=True, timeout=600)
            print(result.stdout, end='', flush=True)
            print(result.stderr, end='', file=sys.stderr, flush=True)
            output = ROOT / 'logs' / Path(base).name
            output.mkdir(mode=0o700)
            subprocess.run(['scp', '-q', '-r', '-o', 'BatchMode=yes', host + ':' + base + '/diagnostics', str(output)], check=True)
            result.check_returncode()
            if 'OVPN_CONFIG_LIVE_PASS' not in result.stdout or 'OVPN_PKI_SCOPED_CLEAN_PASS' not in result.stdout:
                raise ValueError('OVPN_TERMINAL_MARKER_MISSING')
        finally:
            remote_command('rm -rf ' + base + ' && test ! -e ' + base, host=host)
            print('TOOLS_OVPN_SCOPED_CLEAN_PASS host=' + host, flush=True)


def api_boundary():
    import yaml
    host = 'root@192.168.122.243'
    base = '/tmp/tools-api-' + uuid.uuid4().hex[:12]
    manifest = Path('/tmp/kubeauto-tools-api-official/calico-crds.yaml')
    source = json.loads(manifest.with_suffix('.yaml.json').read_text())
    if source['version'] != 'v3.32.4' or source['sha256'] != sha(manifest):
        raise ValueError('TOOLS_API_CRD_SOURCE_REFUSED')
    names = {'globalnetworkpolicies.crd.projectcalico.org', 'hostendpoints.crd.projectcalico.org',
             'clusterinformations.crd.projectcalico.org'}
    crds = [d for d in yaml.safe_load_all(manifest.read_text()) if d and d.get('metadata', {}).get('name') in names]
    if {d['metadata']['name'] for d in crds} != names:
        raise ValueError('TOOLS_API_CRD_INVENTORY_REFUSED')
    for tool, name in (('KubeBackupCli', 'kube-backup'), ('CalicoPolicyCli', 'calico')):
        proof = json.loads((ROOT/'dist'/(name+'-build.json')).read_text())
        path = 'tools/k8stools/'+tool+'.py'
        if proof['inputs'][path] != sha(ROOT/path) or proof['artifacts'][tool] != sha(ROOT/'dist'/(tool+'-rocky8')):
            raise ValueError('TOOLS_API_BUILD_STALE '+tool)
    with remote_lease(host):
        remote_command('test ! -e '+base+' && mkdir -m 700 '+base, host=host)
        output = ROOT/'logs'/Path(base).name
        output.mkdir(mode=0o700)
        try:
            with tempfile.TemporaryDirectory(prefix='tools-api-crds-') as directory:
                selected = Path(directory)/'calico-crds.json'
                selected.write_text(json.dumps(crds))
                files = [(selected, 'calico-crds.json'), (ROOT/'tests/helpers/tools-api-fixture.py', 'fixture.py'),
                         (ROOT/'tests/helpers/run-durable-gate.sh','durable.sh')]
                files += [(ROOT/'dist'/(tool+'-rocky8'), tool) for tool in ('KubeBackupCli','CalicoPolicyCli')]
                for path, name in files:
                    subprocess.run(['scp','-q','-o','BatchMode=yes',str(path),host+':'+base+'/'+name],check=True)
                    actual=remote_command('sha256sum '+base+'/'+name,host=host,capture_output=True).stdout.split()[0]
                    if actual != sha(path):raise ValueError('TOOLS_API_UPLOAD_CHECKSUM_REFUSED '+name)
            process=subprocess.Popen([*SSH,host,'cd '+base+' && bash durable.sh state TOOLS_API_EXIT timeout --signal=TERM --kill-after=30s 900s python3 -B fixture.py'],text=True,
                                     stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            try:
                with (output/'console.log').open('w') as log:
                    for line in process.stdout:
                        log.write(line);log.flush();print(line,end='',flush=True)
                if process.wait(timeout=30):raise ValueError('TOOLS_API_REMOTE_NONZERO')
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=20)
        finally:
            subprocess.run(['scp','-q','-r','-o','BatchMode=yes',host+':'+base+'/diagnostics',str(output)],check=False)
            for suffix in ('exit','finalized'):
                subprocess.run(['scp','-q','-o','BatchMode=yes',host+':'+base+'/state.'+suffix,str(output/('state.'+suffix))],check=False)
            remote_command('test ! -e '+base+'/fixture',host=host)
            remote_command('rm -rf '+base+' && test ! -e '+base,host=host)
            print('TOOLS_API_REMOTE_CLEAN_PASS host='+host,flush=True)
        if any((output/('state.'+suffix)).read_text().strip()!='0' for suffix in ('exit','finalized')):
            raise ValueError('TOOLS_API_DURABLE_REFUSED')
    print('TOOLS_API_BOUNDARY_LIVE_PASS',flush=True)


def authorized_key_update(public, remove=False, path='/root/.ssh/authorized_keys'):
    prefix='from pathlib import Path; p=Path('+repr(path)+'); s=p.read_text(); line='+repr(public)+'; '
    if remove:
        return prefix+'p.write_text("".join(x for x in s.splitlines(keepends=True) if x.rstrip("\\n")!=line)); assert line not in p.read_text()'
    return prefix+'assert not s or s.endswith("\\n"); assert line not in s; p.write_text(s+line+"\\n")'


def publish_load():
    host, target = 'root@192.168.122.243', 'root@192.168.122.2'
    base='/tmp/tools-publish-'+uuid.uuid4().hex[:12]
    product=ROOT/'tools/k8stools/KubePublishCli.py'
    artifact=ROOT/'dist/KubePublishCli-rocky8'
    proof=json.loads((ROOT/'dist/kube-publish-build.json').read_text())
    if proof['inputs']['tools/k8stools/KubePublishCli.py']!=sha(product) or proof['artifacts']['KubePublishCli']!=sha(artifact):
        raise ValueError('PUBLISH_LOAD_BUILD_STALE')
    output=ROOT/'logs'/Path(base).name;output.mkdir(mode=0o700)
    with remote_lease(host), remote_lease(target), tempfile.TemporaryDirectory(prefix='tools-publish-key-') as directory:
        key=Path(directory)/'key'
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-C',Path(base).name,'-f',str(key)],check=True)
        public=key.with_suffix('.pub').read_text().strip()
        # An ephemeral test identity; remove only this exact entry afterwards.
        install=authorized_key_update(public)
        remove=authorized_key_update(public,remove=True)
        remote_command('test ! -e '+base+' && mkdir -m 700 '+base,host=host)
        key_added=False
        try:
            remote_command('python3 -c '+shlex.quote(install),host=target)
            key_added=True
            files=[(key,'key'),(product,'KubePublishCli.py'),(artifact,'KubePublishCli'),
                   (ROOT/'tests/helpers/publish-load-fixture.py','fixture.py'),(ROOT/'tests/helpers/run-durable-gate.sh','durable.sh')]
            for path,name in files:
                subprocess.run(['scp','-q','-o','BatchMode=yes',str(path),host+':'+base+'/'+name],check=True)
                actual=remote_command('sha256sum '+base+'/'+name,host=host,capture_output=True).stdout.split()[0]
                if actual!=sha(path):raise ValueError('PUBLISH_LOAD_UPLOAD_STALE '+name)
            remote_command('chmod 0600 '+base+'/key',host=host)
            process=subprocess.Popen([*SSH,host,'cd '+base+' && bash durable.sh state PUBLISH_LOAD_EXIT timeout --signal=TERM --kill-after=30s 900s python3 -B fixture.py'],
                                     text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            try:
                with (output/'console.log').open('w') as log:
                    for line in process.stdout:log.write(line);log.flush();print(line,end='',flush=True)
                if process.wait(timeout=30):raise ValueError('PUBLISH_LOAD_REMOTE_NONZERO')
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=20)
        finally:
            if key_added:remote_command('python3 -c '+shlex.quote(remove),host=target)
            subprocess.run(['scp','-q','-r','-o','BatchMode=yes',host+':'+base+'/diagnostics',str(output)],check=False)
            for suffix in ('exit','finalized'):
                subprocess.run(['scp','-q','-o','BatchMode=yes',host+':'+base+'/state.'+suffix,str(output/('state.'+suffix))],check=False)
            remote_command('test ! -e '+base+'/fixture',host=host)
            remote_command('rm -rf '+base+' && test ! -e '+base,host=host)
        if any((output/('state.'+suffix)).read_text().strip()!='0' for suffix in ('exit','finalized')):
            raise ValueError('PUBLISH_LOAD_DURABLE_REFUSED')
    print('TOOLS_PUBLISH_LOAD_LIVE_PASS scoped-key=removed',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=('execution-audit-preflight','remote-bootstrap-live','ovpn-config-live','calico-delete-live','publish-load-live'))
    args=parser.parse_args()
    preflight()
    if args.stage=='remote-bootstrap-live':
        remote_bootstrap()
    elif args.stage=='ovpn-config-live':
        ovpn_config()
    elif args.stage=='calico-delete-live':
        api_boundary()
    elif args.stage=='publish-load-live':
        publish_load()
    elif args.stage!='execution-audit-preflight':
        raise SystemExit('TOOLS_BOUNDARY_LIVE_PREREQUISITE_PENDING '+args.stage)


if __name__=='__main__':
    main()
