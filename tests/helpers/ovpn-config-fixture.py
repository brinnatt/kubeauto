#!/usr/bin/env python3
"""Exercise the frozen CLI against an owned Easy-RSA 3.0.7 PKI, without a VPN daemon."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(base):
    return {str(p.relative_to(base)): digest(p) for p in base.rglob('*') if p.is_file()}


def product_environment(base, easy, logs):
    env = dict(os.environ, GENOVPN_OPENVPN_BASE=str(base),
               GENOVPN_EASYRSA_DIR=str(easy), GENOVPN_LOG_DIR=str(logs), NO_COLOR='1')
    # Global batch mode suppresses Easy-RSA's vars diagnostic, which the product
    # preflight checks. Batch belongs to fixture initialization / signing only.
    env.pop('EASYRSA_BATCH', None)
    return env


def refusal_unchanged(command, env, base, expected):
    before = snapshot(base)
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode == 0 or expected not in result.stdout + result.stderr:
        raise ValueError('OVPN_EXPECTED_REFUSAL_MISSING ' + expected)
    if snapshot(base) != before:
        raise ValueError('OVPN_REFUSAL_MUTATED_PKI_OR_BUNDLE')


def verify_revoked_certificate(result):
    if result.returncode == 0 or 'certificate revoked' not in result.stdout + result.stderr:
        raise ValueError('OVPN_REVOKED_CERTIFICATE_ACCEPTED_OR_UNRELATED_ERROR')


def interrupt_crl(tool, user, env, easy, base, logs):
    checkpoint=base/'crl-checkpoint'
    wrapper=base/'openssl-interrupt'
    wrapper.write_text('#!'+sys.executable+'\n'+
        'import os,pathlib,sys,time\n'
        'if "-gencrl" in sys.argv[1:]:\n'
        '    pathlib.Path(os.environ["OVPN_CRL_CHECKPOINT"]).write_text("ready")\n'
        '    while True: time.sleep(.1)\n'
        'os.execv('+repr(shutil.which('openssl'))+', ['+repr(shutil.which('openssl'))+']+sys.argv[1:])\n')
    wrapper.chmod(0o700)
    crl=easy/'pki/crl.pem'
    previous=digest(crl) if crl.exists() else None
    raw=logs/(user+'-interrupted-crl.log')
    with raw.open('w') as stream:
        process=subprocess.Popen([str(tool),'revoke','--user',user],env=dict(env,
            EASYRSA_OPENSSL=str(wrapper),OVPN_CRL_CHECKPOINT=str(checkpoint)),
            stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            deadline=time.monotonic()+60
            while not checkpoint.exists():
                if process.poll() is not None or time.monotonic()>deadline:
                    raise ValueError('OVPN_CRL_INTERRUPTION_CHECKPOINT_MISSING')
                time.sleep(.1)
            os.killpg(process.pid,signal.SIGINT)
            if process.wait(timeout=20)==0:
                raise ValueError('OVPN_INTERRUPTION_REPORTED_SUCCESS')
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=10)
    if (digest(crl) if crl.exists() else None)!=previous:
        raise ValueError('OVPN_INTERRUPTION_REPLACED_EXISTING_CRL')
    rows=[line.split('\t') for line in (easy/'pki/index.txt').read_text().splitlines()
          if line.endswith('/CN='+user)]
    if not rows or rows[-1][0]!='R' or (easy/'pki/issued'/(user+'.crt')).exists():
        raise ValueError('OVPN_INTERRUPTION_CA_STATE_MISMATCH')
    deadline=time.monotonic()+5
    while True:
        try:os.killpg(process.pid,0)
        except ProcessLookupError:break
        if time.monotonic()>deadline:raise ValueError('OVPN_INTERRUPTION_ORPHAN_PROCESS_GROUP')
        time.sleep(.1)
    print('OVPN_CRL_INTERRUPTION_PASS user='+user+' old_crl=preserved ca=revoked processes=reaped',flush=True)


def main():
    root = Path.cwd()
    if not root.name.startswith('tools-ovpn-') or root.parent != Path('/tmp'):
        raise ValueError('OVPN_FIXTURE_SCOPE_REFUSED')
    base = root / 'fixture'
    if base.exists():
        raise ValueError('OVPN_FOREIGN_FIXTURE_REFUSED')
    source = root / 'source'
    proof = json.loads((source / 'source.json').read_text())
    if proof['version'] != 'v3.0.7':
        raise ValueError('OVPN_SOURCE_VERSION_REFUSED')
    for name, expected in proof['files'].items():
        if digest(source / name) != expected:
            raise ValueError('OVPN_SOURCE_CHECKSUM_REFUSED ' + name)
    tool = root / 'OvpnUserCli'
    base.mkdir(mode=0o700)
    try:
        easy = base / '3.0.7'
        shutil.copytree(source, easy)
        (easy / 'vars').write_text('set_var EASYRSA_REQ_CN "Tools isolated CA"\nset_var EASYRSA_KEY_SIZE 2048\n')
        (base / 'server').mkdir()
        logs = root / 'diagnostics'
        logs.mkdir()
        env = product_environment(base, easy, logs)
        def execute(command, name, cwd=None):
            result = subprocess.run([str(x) for x in command], cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=120)
            (logs / (name + '.log')).write_text(result.stdout + result.stderr)
            if result.returncode:
                raise ValueError('OVPN_COMMAND_REFUSED ' + name + ' rc=' + str(result.returncode)
                                 + '\n' + (result.stdout + result.stderr)[-4000:])
            return result.stdout + result.stderr
        execute([easy / 'easyrsa', '--batch', 'init-pki'], 'init', easy)
        execute([easy / 'easyrsa', '--batch', 'build-ca', 'nopass'], 'ca', easy)
        for name in ('certs_by_serial', 'private_by_serial', 'reqs_by_serial'):
            (easy / 'pki/revoked' / name).mkdir(parents=True, exist_ok=True)
        (easy / 'pki/index.txt.attr').write_text('unique_subject = no\n')
        key = base / 'custom shared.key'
        key.write_text('-----BEGIN OpenVPN Static key V1-----\n' +
                       '\n'.join(secrets.token_hex(16) for _ in range(16)) +
                       '\n-----END OpenVPN Static key V1-----\n')
        key.chmod(0o600)
        # Deliberately different obsolete default: the product must consume server.conf.
        (easy / 'pki/ta.key').write_text('obsolete-default-must-not-be-delivered\n')
        conf = base / 'server/server.conf'
        def configure(directive):
            conf.write_text('proto udp\ndev tun\ncipher AES-256-GCM\n' + directive + '\n')
        for user, directive, expected in (
                ('authuser', 'tls-auth "' + str(key) + '" 0', 'tls-auth ta.key 1'),
                ('cryptuser', 'cd "' + str(base) + '"\ntls-crypt "custom shared.key"', 'tls-crypt ta.key')):
            configure(directive)
            execute([tool, 'create', '--user', user, '--rhost', 'vpn.example.invalid'], user + '-create')
            bundle = base / 'client' / user
            text = (bundle / 'client.ovpn').read_text()
            if expected not in text or 'proto udp' not in text or 'dev tun' not in text:
                raise ValueError('OVPN_CLIENT_CONFIGURATION_MISMATCH')
            if digest(bundle / 'ta.key') != digest(key):
                raise ValueError('OVPN_CONFIGURED_KEY_NOT_DELIVERED')
            execute([tool, 'status', '--user', user], user + '-status')
            cert = easy / 'pki/issued' / (user + '.crt')
            execute(['openssl', 'verify', '-CAfile', easy / 'pki/ca.crt', cert], user + '-verify')
            serial = subprocess.check_output(['openssl', 'x509', '-in', str(cert), '-noout', '-serial'], text=True).strip().split('=')[1]
            print('OVPN_CONFIG_CREATE_PASS user=' + user + ' directive=' + expected, flush=True)
            # Existing client bundle and issued certificate must survive a rejected create.
            for bad, message in (('tls-crypt-v2 "' + str(key) + '"', 'tls-crypt-v2'),
                                 ('tls-crypt "' + str(base / 'missing.key') + '"', 'TLS 密钥文件不存在'),
                                 ('tls-auth relative.key', 'TLS 密钥路径不明确')):
                configure(bad)
                refusal_unchanged([str(tool), 'create', '--user', user], env, base, message)
            configure(directive)
            if user=='cryptuser':
                interrupt_crl(tool,user,env,easy,base,logs)
            execute([tool, 'revoke', '--user', user], user + '-revoke')
            if bundle.exists() or cert.exists() or not (easy / 'pki/revoked/certs_by_serial' / (serial + '.crt')).is_file():
                raise ValueError('OVPN_REVOKE_ARCHIVE_MISMATCH')
            execute(['openssl', 'crl', '-in', easy / 'pki/crl.pem', '-noout', '-verify', '-CAfile', easy / 'pki/ca.crt'], user + '-crl')
            result=subprocess.run(['openssl','verify','-CAfile',str(easy/'pki/ca.crt'),
                '-CRLfile',str(easy/'pki/crl.pem'),'-crl_check',
                str(easy/'pki/revoked/certs_by_serial'/(serial+'.crt'))],
                capture_output=True,text=True,env=env,timeout=15)
            (logs/(user+'-crl-rejection.log')).write_text(result.stdout+result.stderr)
            verify_revoked_certificate(result)
            execute([tool, 'status', '--user', user], user + '-revoked-status')
            print('OVPN_CONFIG_REFUSAL_REVOKE_PASS user=' + user, flush=True)
        print('OVPN_CRL_INTERRUPT_RETRY_PASS old_crl=preserved retry=verified revoked_certificate=rejected',flush=True)
        print('OVPN_CONFIG_LIVE_PASS easyrsa=3.0.7 mode=frozen', flush=True)
    finally:
        shutil.rmtree(base)
        if base.exists():
            raise ValueError('OVPN_CLEANUP_INCOMPLETE')
        print('OVPN_PKI_SCOPED_CLEAN_PASS', flush=True)


if __name__ == '__main__':
    main()
