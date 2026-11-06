#!/usr/bin/env python3
"""An independent, lease-owned HTTPS S3 fixture on the source control host.

The cluster never hosts this backend. It exercises S3 repository semantics;
its single MinIO process is not a production HA or durability qualification.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import time
import urllib.request

STATE = Path('/var/tmp/kubeauto-elasticsearch')
NAME = 'kubeauto-elasticsearch-s3'
ENDPOINT = 'https://192.168.122.2:19443'
LABEL = 'kubeauto.io/elasticsearch-s3-owner'
PINS = {'server': 'brinnatt/minio:RELEASE.2025-04-08T15-41-24Z',
        'mc': 'brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z'}


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())


def image(client, key):
    publication = json.loads(Path('/usr/local/kubeauto/tests/evidence/elasticsearch-images-20261103.json').read_text())
    row = next(r for r in publication['images'] if r['key'] == PINS[key])
    ref = row['talkedu_ref'].rsplit(':', 1)[0] + '@' + row['digest']
    client.images.pull(ref)
    return ref


def matching_container(client, record):
    import docker
    try: container = client.containers.get(NAME)
    except docker.errors.NotFound: return None
    if container.labels.get(LABEL) != record['owner'] or record.get('container_id') and container.id != record['container_id']:
        raise ValueError('S3 fixture container ownership differs')
    return container


def save(record):
    path = STATE/'s3.json'
    replacement = path.with_suffix('.new')
    private_json(replacement, record); os.replace(replacement, path)


def mc(client, record, script, *, credentials=None, extra=None, denied=False):
    """Run an owned administration fixture; never relay its secret-bearing logs."""
    credentials = credentials or {'access_key': record['admin_access_key'], 'secret_key': record['admin_secret_key']}
    environment = {'MC_HOST_gate': 'https://' + credentials['access_key'] + ':' + credentials['secret_key'] + '@192.168.122.2:19443'}
    environment.update(extra or {})
    temporary = client.containers.create(image(client, 'mc'), ['-ec', script], entrypoint='/bin/sh',
        name=NAME+'-mc', labels={LABEL: record['owner']}, environment=environment,
        volumes={str(STATE/'s3/client-certs'): {'bind': '/root/.mc/certs', 'mode': 'ro'}}, network_mode='host')
    try:
        temporary.start()
        rc = temporary.wait(timeout=120).get('StatusCode')
        if denied:
            raw = temporary.logs().decode(errors='replace')
            if rc == 0 or not any(message in raw.lower() for message in ('access denied', 'accessdenied', 'invalidaccesskeyid', 'access key id', 'account is disabled')):
                raise ValueError('S3 old credentials lack an explicit authentication refusal')
        elif rc != 0:
            raise ValueError('S3 fixture administration failed')
    finally:
        temporary.remove(force=True)


def rotate(client, action):
    record = json.loads((STATE/'s3.json').read_text())
    if record['owner'] != json.loads((STATE/'ownership.json').read_text())['id'] or matching_container(client, record) is None:
        raise ValueError('S3 credential rotation has no owned backend')
    verify(record)
    if action == 'rotation-prepare':
        if 'rotation' in record: raise ValueError('unrecovered S3 credential rotation')
        record['rotation'] = {'access_key': 'esnext'+secrets.token_hex(8), 'secret_key': secrets.token_urlsafe(32)}
        save(record)  # Own a possibly partially created account before mutation.
        mc(client, record, 'mc admin user add gate "$NEXT_ACCESS" "$NEXT_SECRET" >/dev/null\nmc admin policy attach gate readwrite --user "$NEXT_ACCESS" >/dev/null',
           extra={'NEXT_ACCESS': record['rotation']['access_key'], 'NEXT_SECRET': record['rotation']['secret_key']})
        mc(client, record, 'mc stat gate/elasticsearch-gate >/dev/null', credentials=record['rotation'])
    elif action == 'rotation-disable-old':
        if 'rotation' not in record: raise ValueError('S3 rotation account is missing')
        mc(client, record, 'mc admin user disable gate "$OLD_ACCESS" >/dev/null', extra={'OLD_ACCESS': record['access_key']})
        mc(client, record, 'mc stat gate/elasticsearch-gate', credentials=record, denied=True)
        mc(client, record, 'mc stat gate/elasticsearch-gate >/dev/null', credentials=record['rotation'])
    elif action == 'rotation-restore':
        if 'rotation' not in record: return
        mc(client, record, 'mc admin user enable gate "$OLD_ACCESS" >/dev/null\nmc admin user rm gate "$NEXT_ACCESS" >/dev/null',
           extra={'OLD_ACCESS': record['access_key'], 'NEXT_ACCESS': record['rotation']['access_key']})
        mc(client, record, 'mc stat gate/elasticsearch-gate >/dev/null', credentials=record)
        record.pop('rotation'); save(record)
    else: raise ValueError('invalid S3 rotation action')
    print('ELASTICSEARCH_S3_FIXTURE_ROTATION action='+action, flush=True)


def negative(client, action):
    record=json.loads((STATE/'s3.json').read_text())
    if record['owner']!=json.loads((STATE/'ownership.json').read_text())['id'] or matching_container(client,record) is None:
        raise ValueError('S3 negative fixture has no owned backend')
    verify(record)
    if action=='negative-prepare':
        if 'negative' in record:raise ValueError('S3 negative account has an unrecovered journal')
        token=secrets.token_hex(8)
        account={'access_key':'esnegative'+token,'secret_key':secrets.token_urlsafe(32),'policy':'es-nodelete-'+token,'object':'.negative-'+token}
        record['negative']=account;save(record)
        policy={'Version':'2012-10-17','Statement':[
            {'Effect':'Allow','Action':['s3:*'],'Resource':['arn:aws:s3:::elasticsearch-gate','arn:aws:s3:::elasticsearch-gate/*']},
            {'Effect':'Deny','Action':['s3:DeleteObject'],'Resource':['arn:aws:s3:::elasticsearch-gate/*']}]}
        mc(client,record,'printf "%s" "$POLICY" > /tmp/policy.json\nmc admin policy create gate "$POLICY_NAME" /tmp/policy.json >/dev/null\n'
            'mc admin user add gate "$NEXT_ACCESS" "$NEXT_SECRET" >/dev/null\nmc admin policy attach gate "$POLICY_NAME" --user "$NEXT_ACCESS" >/dev/null',
            extra={'POLICY':json.dumps(policy),'POLICY_NAME':account['policy'],'NEXT_ACCESS':account['access_key'],'NEXT_SECRET':account['secret_key']})
        mc(client,record,'printf probe >/tmp/probe\nmc cp /tmp/probe "gate/elasticsearch-gate/$OBJECT" >/dev/null\n'
            'mc cat "gate/elasticsearch-gate/$OBJECT" >/dev/null',credentials=account,extra={'OBJECT':account['object']})
        mc(client,record,'mc rm "gate/elasticsearch-gate/$OBJECT"',credentials=account,extra={'OBJECT':account['object']},denied=True)
        record['negative']['phase']='ready';save(record)
    elif action=='negative-restore':
        if 'negative' not in record:return
        account=record['negative']
        mc(client,record,'mc rm --force "gate/elasticsearch-gate/$OBJECT" >/dev/null\nmc admin user rm gate "$NEXT_ACCESS" >/dev/null\n'
            'mc admin policy rm gate "$POLICY_NAME" >/dev/null',extra={'OBJECT':account['object'],'NEXT_ACCESS':account['access_key'],'POLICY_NAME':account['policy']})
        mc(client,record,'mc stat gate/elasticsearch-gate >/dev/null',credentials=record)
        record.pop('negative');save(record)
    else:raise ValueError('invalid S3 negative action')
    print('ELASTICSEARCH_S3_FIXTURE_NEGATIVE action='+action,flush=True)


def prepare(client):
    owner = json.loads((STATE/'ownership.json').read_text())['id']
    path, directory = STATE/'s3.json', STATE/'s3'
    if path.exists():
        record = json.loads(path.read_text())
        if record['owner'] != owner: raise ValueError('S3 fixture lease differs')
        container = matching_container(client, record)
        if container is None or container.status != 'running': raise ValueError('retained S3 fixture is not healthy')
        if record.get('phase') != 'ready': raise ValueError('incomplete S3 fixture requires scoped cleanup')
        verify(record)
        return record
    if directory.exists() or directory.is_symlink(): raise ValueError('foreign S3 fixture directory')
    import docker
    try: client.containers.get(NAME)
    except docker.errors.NotFound: pass
    else: raise ValueError('S3 fixture name is already occupied')
    record = {'owner': owner, 'access_key': 'esgate' + secrets.token_hex(8), 'secret_key': secrets.token_urlsafe(32),
              'admin_access_key': 'esadmin'+secrets.token_hex(8), 'admin_secret_key': secrets.token_urlsafe(32),
              'endpoint': ENDPOINT, 'bucket': 'elasticsearch-gate'}
    # Intent precedes creating any data or daemon. The protected journal owns
    # both keys and the exact directory; no secrets are written to source.
    private_json(path, record)
    directory.mkdir(mode=0o700)
    record['directory_identity'] = [directory.stat().st_dev, directory.stat().st_ino]
    replacement = path.with_suffix('.new'); private_json(replacement, record); os.replace(replacement, path)
    for child in ('data', 'certs', 'client-certs'):
        (directory/child).mkdir(mode=0o700)
    certs = directory/'certs'
    command = ['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-sha256', '-days', '3',
               '-keyout', str(certs/'private.key'), '-out', str(certs/'public.crt'), '-subj', '/CN=elasticsearch-gate-s3',
               '-addext', 'subjectAltName=IP:192.168.122.2', '-addext', 'basicConstraints=critical,CA:TRUE']
    result = subprocess.run(command, capture_output=True, timeout=30)
    if result.returncode: raise ValueError('S3 fixture certificate generation failed')
    (certs/'private.key').chmod(0o600)
    # mc trusts only this fixture CA through its documented CAs directory.
    (directory/'client-certs/CAs').mkdir()
    (directory/'client-certs/CAs/fixture.crt').write_bytes((certs/'public.crt').read_bytes())
    server = image(client, 'server')
    container = client.containers.run(server, ['server', '/data', '--address', ':9000', '--console-address', ':9001'],
        name=NAME, detach=True, labels={LABEL: owner}, user='0:0',
        environment={'MINIO_ROOT_USER': record['admin_access_key'], 'MINIO_ROOT_PASSWORD': record['admin_secret_key']},
        ports={'9000/tcp': ('192.168.122.2', 19443)}, mem_limit='1g', nano_cpus=1000000000,
        volumes={str(directory/'data'): {'bind': '/data', 'mode': 'rw'}, str(certs): {'bind': '/root/.minio/certs', 'mode': 'ro'}})
    record['container_id'] = container.id
    replacement = path.with_suffix('.new'); private_json(replacement, record); os.replace(replacement, path)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try: verify(record); break
        except (OSError, ValueError):
            print('ELASTICSEARCH_WAIT_HEARTBEAT fixture=https-s3', flush=True); time.sleep(3)
    else: raise ValueError('S3 fixture HTTPS did not become ready')
    mc(client, record, 'mc mb --ignore-existing gate/elasticsearch-gate >/dev/null\nmc admin user add gate "$DATA_ACCESS" "$DATA_SECRET" >/dev/null\nmc admin policy attach gate readwrite --user "$DATA_ACCESS" >/dev/null',
       extra={'DATA_ACCESS': record['access_key'], 'DATA_SECRET': record['secret_key']})
    mc(client, record, 'mc stat gate/elasticsearch-gate >/dev/null', credentials=record)
    record['phase'] = 'ready'; save(record)
    print('ELASTICSEARCH_S3_FIXTURE_READY backend=independent-control-host HTTPS=verified', flush=True)
    return record


def verify(record):
    context = ssl.create_default_context(cafile=str(STATE/'s3/certs/public.crt'))
    with urllib.request.urlopen(record['endpoint'] + '/minio/health/ready', context=context, timeout=5) as response:
        if response.status != 200: raise ValueError('S3 fixture health refused')


def cleanup(client):
    import shutil
    path = STATE/'s3.json'
    if not path.exists():
        if (STATE/'s3').exists(): raise ValueError('S3 data has no ownership journal')
        return
    record = json.loads(path.read_text())
    owner = json.loads((STATE/'ownership.json').read_text())['id']
    if record['owner'] != owner: raise ValueError('S3 cleanup lease differs')
    directory = STATE/'s3'
    if directory.is_symlink() or directory.exists() and record.get('directory_identity') != [directory.stat().st_dev, directory.stat().st_ino]:
        raise ValueError('S3 fixture directory was replaced')
    container = matching_container(client, record)
    import docker
    try: mc = client.containers.get(NAME+'-mc')
    except docker.errors.NotFound: mc = None
    else:
        if mc.labels.get(LABEL) != owner: raise ValueError('foreign S3 client fixture')
    if mc: mc.remove(force=True)
    if container: container.remove(force=True)
    if directory.exists(): shutil.rmtree(directory)
    path.unlink()
    print('ELASTICSEARCH_S3_FIXTURE_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'cleanup', 'rotation-prepare', 'rotation-disable-old', 'rotation-restore','negative-prepare','negative-restore'))
    args = parser.parse_args()
    if os.environ.get('ELASTICSEARCH_PARENT_LEASE_HELD') != 'yes': raise ValueError('fixed source/control lease required')
    import docker
    client = docker.from_env()
    try:
        if args.action.startswith('rotation-'): rotate(client, args.action)
        elif args.action.startswith('negative-'): negative(client,args.action)
        else: globals()[args.action](client)
    finally: client.close()


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('ELASTICSEARCH_S3_FIXTURE_FAIL reason=' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
