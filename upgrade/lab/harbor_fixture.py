"""Private local Harbor business ledger, owned by the upgrade lab runner.

Creation records identities once. Verification never recreates missing objects,
changes permissions or refreshes credentials; the original ledger remains the
authority across a migration, a logical transfer and a complete restore.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tarfile
import tempfile


CONTENT = b'kubeauto Harbor confirmed pre-upgrade payload\n'
PROJECT = 'upgrade-private'
REPOSITORY = PROJECT + '/confirmed'


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                           0o600), 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


def read_private(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o777 != 0o600:
        raise ValueError('private owner-only fixture input required')
    return json.loads(path.read_text())


class Ledger:
    def __init__(self, settings, state):
        self.settings = settings
        self.state = Path(state)
        if self.state.is_symlink() or self.state.stat().st_uid != os.geteuid() or self.state.stat().st_mode & 0o777 != 0o700:
            raise ValueError('private runner-owned ledger directory required')
        self.authority = settings['hostname'] + ':' + str(settings['port'])
        self.image = self.authority + '/' + REPOSITORY + ':confirmed'
        self.admin = ('admin', settings['password'])

    def api(self, method, path, data=None, identity=None, expected=(200,)):
        context = ssl.create_default_context(cafile=self.settings['ca'])
        connection = http.client.HTTPSConnection(self.settings['hostname'], self.settings['port'],
                                                 context=context, timeout=15)
        headers = {'Content-Type': 'application/json', 'X-Is-Resource-Name': 'true'}
        if identity is not None:
            headers['Authorization'] = 'Basic ' + base64.b64encode(':'.join(identity).encode()).decode()
        try:
            local = socket.create_connection(('127.0.0.1', self.settings['port']), timeout=15)
            try: connection.sock = context.wrap_socket(local, server_hostname=self.settings['hostname'])
            except BaseException:
                local.close(); raise
            connection.request(method, '/api/v2.0/' + path,
                               json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            body = response.read()
            if response.status not in expected:
                # Response bodies and identities may contain credentials.
                raise ValueError('Harbor fixture API refused ' + method + ' status=' + str(response.status))
            return json.loads(body) if body else None
        finally:
            connection.close()

    def docker(self, *arguments, data=None, identity=None, expected_success=True):
        with tempfile.TemporaryDirectory(prefix='docker-client-', dir=self.state) as directory:
            environment = dict(os.environ, DOCKER_CONFIG=directory)
            if identity is not None:
                login = subprocess.run([self.settings['docker'], 'login', self.authority,
                    '--username', identity[0], '--password-stdin'], input=identity[1].encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=60)
                if login.returncode:
                    raise ValueError('Harbor fixture client authentication failed')
            result = subprocess.run([self.settings['docker'], *arguments], input=data,
                                    capture_output=True, env=environment, timeout=180)
            if (result.returncode == 0) != expected_success:
                raise ValueError('Harbor fixture Docker operation has an unexpected outcome: ' + arguments[0])
            if not expected_success and not re.search(rb'(?i)denied|unauthorized|insufficient.scope', result.stdout+result.stderr):
                raise ValueError('the negative push did not prove an authorization refusal')
            return result.stdout

    def artifact(self):
        return self.api('GET', 'projects/'+PROJECT+'/repositories/confirmed/artifacts/confirmed',
                        identity=self.admin)

    @staticmethod
    def membership(rows):
        return [{key: row[key] for key in ['id', 'entity_id', 'role_id', 'entity_name']}
                for row in rows]

    def remove_local_fixture(self, record):
        references = [self.image, self.authority+'/'+REPOSITORY+'@'+record['digest']]
        rows = self.docker('image', 'ls', '--no-trunc', '--format', '{{json .}}').decode().splitlines()
        # Inventory the exact fixture tag. A changed local tag is not ours to
        # delete. Digest references are checked through Docker inspect below.
        for row in map(json.loads, rows):
            if row['Repository']+':'+row['Tag'] == self.image:
                if row['ID'] != record['image_id']:
                    raise ValueError('local fixture tag was independently replaced')
                self.docker('image', 'rm', self.image)
        digests = self.docker('image', 'ls', '--digests', '--no-trunc', '--format', '{{json .}}').decode().splitlines()
        for row in map(json.loads, digests):
            if row['Repository'] == self.authority+'/'+REPOSITORY and row['Digest'] == record['digest']:
                if row['ID'] != record['image_id']:
                    raise ValueError('local fixture digest identifies a different image')
                self.docker('image', 'rm', references[1])
                break
        remaining = self.docker('image', 'ls', '--all', '--no-trunc', '--format', '{{json .}}').decode().splitlines()
        if any(row['ID'] == record['image_id'] for row in map(json.loads, remaining)):
            raise ValueError('fixture image remains cached through another local reference')

    def create(self):
        if (self.state/'ledger.json').exists():
            raise ValueError('original ledger already exists; creation cannot overwrite it')
        # Every create must succeed as a new object. Existing objects are never
        # silently adopted and a partially failed setup must be inspected.
        self.api('POST', 'projects', {'project_name': PROJECT, 'metadata': {'public': 'false'}},
                 self.admin, (201,))
        password = secrets.token_urlsafe(20) + 'Aa1!'
        self.api('POST', 'users', {'username': 'upgrade-reader', 'password': password,
            'email': 'upgrade-reader@example.invalid', 'realname': 'Upgrade reader'}, self.admin, (201,))
        self.api('POST', 'projects/'+PROJECT+'/members',
                 {'role_id': 3, 'member_user': {'username': 'upgrade-reader'}}, self.admin, (201,))
        robot = self.api('POST', 'robots', {'name': 'upgrade-pull', 'level': 'project', 'duration': -1,
            'permissions': [{'kind': 'project', 'namespace': PROJECT,
                             'access': [{'resource': 'repository', 'action': 'pull'}]}]}, self.admin, (201,))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            member = tarfile.TarInfo('confirmed.txt'); member.size = len(CONTENT)
            member.mode = 0o644; member.mtime = 0
            archive.addfile(member, io.BytesIO(CONTENT))
        image_id = self.docker('image', 'import', '-', self.image, data=stream.getvalue()).decode().strip()
        self.docker('push', self.image, identity=self.admin)
        project = self.api('GET', 'projects/'+PROJECT, identity=self.admin)
        member = self.api('GET', 'projects/'+PROJECT+'/members?entityname=upgrade-reader', identity=self.admin)
        artifact = self.artifact()
        record = {'schema_version': 1, 'project_id': project['project_id'], 'members': self.membership(member),
                  'digest': artifact['digest'], 'artifact_id': artifact['id'], 'image_id': image_id,
                  'payload_sha256': hashlib.sha256(CONTENT).hexdigest(),
                  'reader': ['upgrade-reader', password],
                  'robot': [robot['name'], robot['secret']], 'robot_id': robot['id']}
        private_json(self.state/'ledger.json', record)
        return self.verify()

    def verify(self):
        record = read_private(self.state/'ledger.json')
        project = self.api('GET', 'projects/'+PROJECT, identity=self.admin)
        if project['project_id'] != record['project_id'] or project['metadata'].get('public') != 'false':
            raise ValueError('original private project identity or visibility changed')
        members = self.api('GET', 'projects/'+PROJECT+'/members?entityname=upgrade-reader', identity=self.admin)
        if self.membership(members) != record['members']:
            raise ValueError('original project membership or role changed')
        robot = self.api('GET', 'robots/'+str(record['robot_id']), identity=self.admin)
        if robot['name'] != record['robot'][0] or robot.get('disable'):
            raise ValueError('original robot identity changed')
        artifact = self.artifact()
        if artifact['id'] != record['artifact_id'] or artifact['digest'] != record['digest']:
            raise ValueError('original tag or artifact identity changed')
        self.api('GET', 'projects/'+PROJECT, identity=None, expected=(401, 403, 404))
        for identity in [record['reader'], record['robot']]:
            self.remove_local_fixture(record)
            # The source image is absent locally before each pull. This makes
            # missing server blobs fail instead of being hidden by the cache.
            self.docker('pull', self.authority+'/'+REPOSITORY+'@'+record['digest'], identity=identity)
            self.docker('image', 'tag', self.authority+'/'+REPOSITORY+'@'+record['digest'], self.image)
            self.docker('push', self.image, identity=identity, expected_success=False)
            saved = self.docker('image', 'save', self.authority+'/'+REPOSITORY+'@'+record['digest'])
            with tarfile.open(fileobj=io.BytesIO(saved)) as archive:
                manifest = json.load(archive.extractfile('manifest.json'))
                if len(manifest) != 1 or len(manifest[0]['Layers']) != 1:
                    raise ValueError('fixture image layer identity changed')
                with tarfile.open(fileobj=archive.extractfile(manifest[0]['Layers'][0])) as layer:
                    if layer.extractfile('confirmed.txt').read() != CONTENT:
                        raise ValueError('confirmed image bytes changed')
        self.remove_local_fixture(record)
        return {'marker': 'HARBOR_BUSINESS_LEDGER_PASS', 'project_id': record['project_id'],
                'artifact_id': record['artifact_id'], 'digest': record['digest'],
                'payload_sha256': record['payload_sha256'],
                'assertions': ['private-project-and-membership-preserved', 'original-robot-secret-usable',
                               'reader-and-robot-pull-allowed-push-denied', 'original-manifest-and-payload-preserved']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'verify'])
    parser.add_argument('--settings', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    try:
        ledger = Ledger(read_private(args.settings), args.state)
        result = getattr(ledger, args.action)()
        private_json(args.result, result)
        print(result['marker'])
    except Exception:
        # Do not echo response bodies, command output or secret-bearing input.
        raise SystemExit('Harbor business fixture failed; preserve its private ledger and runner diagnostics') from None


if __name__ == '__main__':
    main()
