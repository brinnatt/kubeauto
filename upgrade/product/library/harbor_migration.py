#!/usr/bin/python
"""Stopped, identity-bound Harbor backup and supported Compose migration.

Ansible supplies the parsed original harbor.yml; no YAML dependency is needed
on the managed host. All command output stays in a private local diagnostic log.
The public result carries identities and checkpoints, never configuration secrets.
"""
from contextlib import contextmanager
import base64
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import shutil
import ssl
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid


PACKAGES = {
    'v2.14.6': {'amd64': 'f3adaff30c4531c58f9d6ee8c5164f95f08582651c12b698b92be9e98df55cf6'},
    'v2.16.0': {'amd64': '643c137d8e829c857ef0ba45c46b5d6d8553077340bbd4929ded7e92808e77ff',
                 'arm64': '6525af9c26cc24e48ecf37d3f7d9d139895b39a037a3de697f995171ce65524a'},
}


class Refused(ValueError):
    pass


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def real_path(value):
    path = Path(value)
    if not path.is_absolute() or path != path.resolve() or path == Path('/'):
        raise Refused('absolute canonical paths without symbolic links are required')
    return path


def within(path, parent):
    return path == parent or parent in path.parents


def atomic_json(path, data):
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream, sort_keys=True, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def interruptible():
    """Turn controller termination into a recoverable, checkpointed failure."""
    def interrupted(signum, frame):
        # subprocess/selectors may retry InterruptedError as an interrupted
        # system call. A project refusal must unwind and reap the command.
        raise Refused('Harbor maintenance interrupted; preserve the private checkpoint and diagnostics')

    previous = {signum: signal.signal(signum, interrupted)
                for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


class Harbor:
    def __init__(self, *, data_directory, backup_directory, binary_directory,
                 configuration, ca_file, packages=None, transfer_directory=None):
        self.data = real_path(data_directory)
        self.backup = real_path(backup_directory)
        if within(self.backup, self.data) or within(self.data, self.backup):
            raise Refused('backup must be outside the complete Harbor data tree')
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(self.data)):
            raise Refused('the official prepare script requires a data path without spaces or quotes')
        self.install = self.data / 'harbor'
        self.docker = real_path(binary_directory) / 'docker'
        self.compose = real_path(binary_directory) / 'docker-compose'
        self.config = configuration
        if not isinstance(self.config, dict) or self.config.get('data_volume') != str(self.data):
            raise Refused('the original configuration must own this exact data volume')
        if any(self.config.get(key) for key in ['external_database', 'external_redis', 'external_url']):
            raise Refused('external services require their own coordinated migration; no local-only snapshot')
        storage = self.config.get('storage_service') or {}
        if set(storage) - {'filesystem', 'redirect', 'ca_bundle'}:
            raise Refused('external registry storage is outside a filesystem-consistent snapshot')
        if (self.config.get('internal_tls') or {}).get('enabled'):
            raise Refused('internal TLS identity migration requires its separate certificate inventory')
        https = self.config.get('https') or {}
        host = self.config.get('hostname', '')
        port = https.get('port')
        if not re.fullmatch(r'[A-Za-z0-9.-]+', str(host)) or type(port) is not int or not 1 <= port <= 65535:
            raise Refused('an explicit HTTPS hostname and port are required')
        self.endpoint = 'https://' + host + ':' + str(port)
        self.tls = {key: real_path(https.get(key, '')) for key in ['certificate', 'private_key']}
        self.ca = real_path(ca_file)
        if any(within(self.backup, path.parent) for path in [*self.tls.values(), self.ca]):
            raise Refused('backup must not be inside the TLS input directory')
        self.arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
        if self.arch is None:
            raise Refused('unsupported host architecture')
        self.package_paths = {version: real_path(path) for version, path in (packages or {}).items()}
        self.receipt = self.backup / 'receipt.json'
        self.transfer = real_path(transfer_directory) if transfer_directory else None

    @contextmanager
    def lease(self):
        path = self.data.parent / ('.kubeauto-harbor-' + hashlib.sha256(str(self.data).encode()).hexdigest()[:16] + '.lock')
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise Refused('another operation owns this Harbor data tree') from None
            yield
        finally:
            os.close(descriptor)

    def command(self, args, *, cwd=None, timeout=600):
        """Capture failure output privately; never echo a secret-bearing command."""
        process = subprocess.Popen([str(arg) for arg in args], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        interrupted = None
        try:
            output, errors = process.communicate(timeout=timeout)
        except BaseException as error:
            timed_out = isinstance(error, subprocess.TimeoutExpired)
            interrupted = error
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try:
                output, errors = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                output, errors = process.communicate()
        if self.backup.is_dir():
            descriptor = os.open(self.backup/'commands.log', os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'ab') as log:
                log.write(output); log.write(errors)
            failure = self.backup/'first-failure.json'
            if (timed_out or interrupted is not None or process.returncode) and not failure.exists():
                atomic_json(failure, {'command': [str(arg) for arg in args],
                    'cwd': str(cwd) if cwd else None, 'returncode': process.returncode,
                    'timed_out': timed_out, 'interrupted': interrupted is not None and not timed_out})
        if timed_out:
            raise Refused('Harbor command timed out; preserve the private checkpoint and diagnostics')
        if interrupted is not None:
            raise interrupted
        if process.returncode:
            raise Refused('Harbor command failed rc=' + str(process.returncode) + '; preserve the private checkpoint and diagnostics')
        return output.decode()

    def composition(self):
        value = json.loads(self.command([self.compose, '-f', self.install/'docker-compose.yml', 'config', '--format', 'json'], cwd=self.install))
        services = value.get('services', {})
        if not {'core', 'proxy', 'registry', 'postgresql', 'redis'} <= set(services):
            raise Refused('the project filesystem/internal-database Compose services are required')
        for name, target, source in [('core', '/data', self.data),
                ('registry', '/storage', self.data/'registry'),
                ('postgresql', '/var/lib/postgresql/data', self.data/'database'),
                ('redis', '/var/lib/redis', self.data/'redis')]:
            mounts = [v for v in services[name].get('volumes', []) if v.get('target') == target]
            if (len(mounts) != 1 or mounts[0].get('type') != 'bind' or
                    not isinstance(mounts[0].get('source'), str) or
                    Path(mounts[0]['source']) != source):
                raise Refused('a persistent Compose mount differs from the declared data tree')
        return value

    def save_queue(self, composition):
        """Persist quiesced Redis state before the complete stopped archive."""
        objects = self.containers(composition)
        for obj in objects:
            service = obj['Config']['Labels']['com.docker.compose.service']
            if service not in {'redis', 'postgresql', 'log'} and obj['State']['Running']:
                raise Refused('Harbor writers must be stopped before its queue snapshot')
        cache = composition['services']['redis']
        if cache.get('ports'):
            raise Refused('a published internal queue endpoint requires independent writer isolation')
        valkey = '/valkey-photon:' in cache['image']
        args = [self.docker, 'exec', cache['container_name'],
                'valkey-cli' if valkey else 'redis-cli', '--raw']
        def fields(section):
            return dict(line.split(':', 1) for line in
                        self.command(args+['INFO', section]).splitlines()
                        if ':' in line and not line.startswith('#'))
        info = fields('server')
        version = info.get('valkey_version' if valkey else 'redis_version', '')
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
            raise Refused('the real queue engine/version could not be inventoried')
        if not valkey and tuple(map(int, version.split('.')[:2])) > (7, 2):
            raise Refused('Valkey does not promise RDB compatibility with Redis 7.4 or later')
        for key, value in [('dir', '/var/lib/redis'), ('dbfilename', 'dump.rdb'), ('appendonly', 'no')]:
            if self.command(args+['CONFIG', 'GET', key]).splitlines() != [key, value]:
                raise Refused('custom queue persistence requires its own coordinated recovery path')
        if self.command(args+['SAVE'], timeout=600).strip() != 'OK':
            raise Refused('Harbor queue snapshot failed; preserve the stopped writer boundary')
        persistence = fields('persistence')
        if (persistence.get('rdb_last_bgsave_status') != 'ok' or
                persistence.get('rdb_changes_since_last_save') != '0'):
            raise Refused('Harbor queue snapshot did not reach a clean persisted boundary')
        return {'engine': 'valkey' if valkey else 'redis', 'version': version,
                'keyspace': fields('keyspace')}

    def containers(self, composition, *, running=None):
        ids = self.command([self.docker, 'ps', '-aq']).split()
        objects = json.loads(self.command([self.docker, 'inspect', *ids])) if ids else []
        names = {service['container_name'] for service in composition['services'].values()}
        owned = []
        for obj in objects:
            name = obj['Name'].removeprefix('/')
            touches = any(within(Path(m['Source']), self.data) or within(self.data, Path(m['Source']))
                          for m in obj.get('Mounts', []) if m.get('Type') == 'bind')
            if name not in names:
                if touches: raise Refused('a foreign container uses the Harbor data tree')
                continue
            labels = obj.get('Config', {}).get('Labels', {})
            service = labels.get('com.docker.compose.service')
            expected = composition['services'].get(service, {})
            if (labels.get('com.docker.compose.project.working_dir') != str(self.install) or
                    expected.get('container_name') != name or obj['Config']['Image'] != expected.get('image')):
                raise Refused('a Harbor container name is owned by a different installation or image')
            if running is not None and obj['State']['Running'] is not running:
                raise Refused('Harbor containers are not all in the required running/stopped state')
            owned.append(obj)
        if running is True and {o['Name'].removeprefix('/') for o in owned} != names:
            raise Refused('the existing Harbor installation is incomplete')
        return owned

    def api(self, suffix):
        # A replacement host may not yet own production DNS. Always inspect
        # this managed host's listener, retaining the configured TLS identity.
        # Resolving the hostname here could accept the untouched source Harbor.
        headers = {}
        if suffix == 'systeminfo':
            # Harbor deliberately omits protected fields (including its
            # version) from an anonymous HTTP 200 response.
            password = self.config.get('harbor_admin_password')
            if not isinstance(password, str) or not password:
                raise Refused('the original Harbor administrator credential is required for version inventory')
            headers['Authorization'] = 'Basic ' + base64.b64encode(
                ('admin:' + password).encode()).decode()
        context = ssl.create_default_context(cafile=str(self.ca))
        host = self.config['hostname']
        connection = http.client.HTTPSConnection(host, self.config['https']['port'],
                                                context=context, timeout=10)
        try:
            local = socket.create_connection(('127.0.0.1', connection.port), timeout=10)
            try: connection.sock = context.wrap_socket(local, server_hostname=host)
            except BaseException:
                local.close()
                raise
            connection.request('GET', '/api/v2.0/'+suffix, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise Refused('the local Harbor API did not return HTTP 200')
            return json.load(response)
        finally:
            connection.close()

    def health(self, version):
        actual = self.api('systeminfo').get('harbor_version', '')
        if actual != version and not actual.startswith(version+'-'):
            raise Refused('the live Harbor version differs from the migration boundary')
        if self.api('health').get('status') != 'healthy':
            raise Refused('Harbor is not completely healthy')

    def checkpoint(self, receipt, phase, **extra):
        receipt.update(phase=phase, **extra)
        atomic_json(self.receipt, receipt)

    def load(self, *, stopped=False):
        if (self.backup.is_symlink() or stat.S_IMODE(self.backup.stat().st_mode) != 0o700 or
                self.backup.stat().st_uid != os.geteuid()):
            raise Refused('the private original backup directory is required')
        if (self.receipt.is_symlink() or not self.receipt.is_file() or
                stat.S_IMODE(self.receipt.stat().st_mode) != 0o600 or
                self.receipt.stat().st_uid != os.geteuid()):
            raise Refused('the private original backup receipt is required')
        receipt = json.loads(self.receipt.read_text())
        machine = digest('/etc/machine-id')
        if receipt.get('machine') != machine or receipt.get('data_directory') != str(self.data) or receipt.get('architecture') != self.arch:
            raise Refused('the backup belongs to another host, architecture or data tree')
        identity = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        if (receipt.get('configuration_sha256') != identity or receipt.get('endpoint') != self.endpoint or
                receipt.get('tls_paths') != {**{k: str(v) for k,v in self.tls.items()}, 'ca': str(self.ca)}):
            raise Refused('the original configuration, endpoint or TLS inputs differ from this backup')
        if stopped:
            if (receipt.get('phase') != 'stopped' or receipt.get('files') != {} or receipt.get('tls') != {} or
                    any((self.backup/name).exists() or (self.backup/name).is_symlink()
                        for name in ['data.tar','images.tar','portable.json','registry.dump'])):
                raise Refused('backup already exists; inspect its durable checkpoint instead of replacing it')
            return receipt
        if receipt.get('phase') not in ['backup-complete', 'restore-stop-intent', 'restore-extract-intent',
                'restored', 'upgrade-intent', 'v2.14.6-ready', 'v2.16.0-ready',
                'portable-export-intent', 'portable-export-complete']:
            raise Refused('the stopped backup is incomplete; preserve its first failure')
        if not {'data.tar', 'images.tar'} <= set(receipt.get('files', {})):
            raise Refused('both the complete data and original image archives are required')
        for filename, checksum in receipt.get('files', {}).items():
            path = self.backup/filename
            if (Path(filename).name != filename or path.is_symlink() or not path.is_file() or
                    stat.S_IMODE(path.stat().st_mode) != 0o600 or digest(path) != checksum):
                raise Refused('a complete original backup file is missing or changed')
        return receipt

    def portable_export(self):
        """Export only a quiesced internal registry DB; retain the original host."""
        receipt = self.load()
        if receipt['source'] not in ['v2.13.0', 'v2.16.0'] or receipt['phase'] not in ['backup-complete', 'portable-export-intent']:
            raise Refused('portable export requires a fresh complete stopped 2.13.0 or 2.16.0 backup')
        if (self.backup/'registry.dump').exists() or (self.backup/'portable.json').exists():
            raise Refused('portable export output already exists; inspect or restore instead of overwriting it')
        composition = receipt['composition']
        objects = self.containers(composition, running=False)
        if sorted(o['Id'] for o in objects) != receipt['source_container_ids']:
            raise Refused('the stopped source identities changed before logical export')
        database = next(o['Id'] for o in objects
                        if o['Config']['Labels']['com.docker.compose.service'] == 'postgresql')
        logging = next(o['Id'] for o in objects
                       if o['Config']['Labels']['com.docker.compose.service'] == 'log')
        remote = '/tmp/kubeauto-registry-' + uuid.uuid4().hex + '.dump'
        self.checkpoint(receipt, 'portable-export-intent', export_container=database, export_file=remote)
        dump = self.backup/'registry.dump'
        sql = [self.docker, 'exec', '--user', 'postgres', database, 'psql', '-U', 'postgres', '-d', 'registry', '-At', '-v', 'ON_ERROR_STOP=1', '-c']
        try:
            # Official Harbor containers use the local syslog driver. Its
            # listener must be available before Docker can start PostgreSQL;
            # keep all application writers stopped throughout the export.
            self.command([self.compose, '-f', self.install/'docker-compose.yml', 'up', '-d',
                '--no-deps', '--wait', '--wait-timeout', '120', 'log'], cwd=self.install, timeout=180)
            self.command([self.compose, '-f', self.install/'docker-compose.yml', 'up', '-d',
                '--no-deps', '--wait', '--wait-timeout', '120', 'postgresql'], cwd=self.install, timeout=180)
            # This path owns the project default single DB/role. Do not lose
            # custom databases or SQL identities by silently omitting globals.
            major = self.command(sql+['SHOW server_version_num']).strip()
            expected = '15' if receipt['source'] == 'v2.13.0' else '18'
            if not major.isdigit() or int(major)//10000 != int(expected):
                raise Refused('the source PostgreSQL major differs from the inventoried portable path')
            if self.command(sql+["SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY datname"]).strip() != 'registry':
                raise Refused('portable export refuses additional non-project databases')
            if self.command(sql+["SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname"]).strip() != 'postgres':
                raise Refused('portable export refuses additional SQL roles requiring a coordinated globals restore')
            if self.command(sql+["SELECT spcname FROM pg_tablespace WHERE spcname NOT IN ('pg_default', 'pg_global')"]).strip():
                raise Refused('portable export refuses custom tablespaces requiring a coordinated globals restore')
            if self.command(sql+["SELECT 1 FROM pg_db_role_setting WHERE setdatabase = 0 UNION ALL SELECT 1 FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = 'postgres')"]).strip():
                raise Refused('portable export refuses cluster-wide role settings or memberships absent from a database dump')
            schema = self.command(sql+['SELECT version, dirty FROM schema_migrations']).strip()
            if not re.fullmatch(r'[0-9]+\|f', schema):
                raise Refused('the original Harbor schema is dirty or unknown')
            self.command([self.docker, 'exec', '--user', 'postgres', database, 'pg_dump',
                '-U', 'postgres', '-d', 'registry', '--format=custom', '--file='+remote], timeout=3600)
            self.command([self.docker, 'cp', database+':'+remote, dump], timeout=3600)
            dump.chmod(0o600)
            with dump.open('rb') as stream:
                if dump.stat().st_size < 5 or stream.read(5) != b'PGDMP':
                    raise Refused('the portable export is not a PostgreSQL custom archive')
        finally:
            try:
                self.command([self.docker, 'exec', '--user', 'postgres', database, 'rm', '-f', '--', remote], timeout=30)
            finally:
                self.command([self.docker, 'stop', '--time', '60', database, logging], timeout=90)
        if sorted(o['Id'] for o in self.containers(composition, running=False)) != receipt['source_container_ids']:
            raise Refused('the original source did not return to its stopped export boundary')
        files = {name: checksum for name, checksum in receipt['files'].items() if name != 'images.tar'}
        files['registry.dump'] = digest(dump)
        portable = {key: receipt[key] for key in ['source', 'architecture', 'data_directory',
            'original_configuration', 'source_config_sha256', 'configuration_sha256', 'endpoint',
            'tls_paths', 'tls', 'composition', 'queue']}
        portable.update(schema_version=1, kind='harbor-portable-export', files=files,
            postgres_major=int(expected), schema_identity=schema, source_machine=receipt['machine'],
            source_recovery=str(self.backup))
        atomic_json(self.backup/'portable.json', portable)
        self.checkpoint(receipt, 'portable-export-complete', portable_sha256=digest(self.backup/'portable.json'))
        return {'changed': True, 'marker': 'HARBOR_PORTABLE_EXPORT_PASS', 'source': receipt['source'],
                'architecture': self.arch, 'transfer': str(self.backup), 'source_state': 'stopped; original full recovery point retained'}

    def portable_input(self):
        """Verify the transferred private export before creating target state."""
        path = self.transfer
        if path is None or within(path, self.data) or within(self.data, path) or path == self.backup:
            raise Refused('a separate canonical transfer directory is required')
        if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise Refused('the transferred export directory must be private and owned by this operator')
        manifest = path/'portable.json'
        if manifest.is_symlink() or manifest.stat().st_uid != os.geteuid() or stat.S_IMODE(manifest.stat().st_mode) != 0o600:
            raise Refused('the private original portable manifest is required')
        value = json.loads(manifest.read_text())
        if (value.get('kind') != 'harbor-portable-export' or value.get('schema_version') != 1 or
                value.get('source') not in ['v2.13.0', 'v2.16.0'] or
                value.get('architecture') not in ['amd64', 'arm64'] or
                value.get('original_configuration') != self.config or value.get('endpoint') != self.endpoint or
                value.get('data_directory') != str(self.data) or
                value.get('tls_paths') != {**{k: str(v) for k,v in self.tls.items()}, 'ca': str(self.ca)} or
                value.get('configuration_sha256') != hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()):
            raise Refused('portable source version, configuration or data identity differs')
        files = value.get('files', {})
        if not {'data.tar', 'registry.dump'} <= set(files):
            raise Refused('the stopped data and logical database export are both required')
        if (value.get('postgres_major') != (15 if value['source'] == 'v2.13.0' else 18) or
                not re.fullmatch(r'[0-9]+\|f', str(value.get('schema_identity', ''))) or
                set(value.get('tls', {})) != {str(p) for p in [*self.tls.values(), self.ca]} or
                any(info.get('file') not in files for info in value.get('tls', {}).values())):
            raise Refused('portable database or complete TLS identity inventory differs')
        for name, checksum in files.items():
            file = path/name
            if (Path(name).name != name or file.is_symlink() or not file.is_file() or file.stat().st_uid != os.geteuid() or
                    stat.S_IMODE(file.stat().st_mode) != 0o600 or digest(file) != checksum):
                raise Refused('a portable export input is missing, unsafe or changed')
        with tarfile.open(path/'data.tar') as archive:
            for member in archive:
                parts = Path(member.name).parts
                if (Path(member.name).is_absolute() or not parts or parts[0] != self.data.name or
                        '..' in parts or not (member.isfile() or member.isdir())):
                    raise Refused('portable data archive contains unsupported links or escaping members')
            original = archive.extractfile(self.data.name+'/harbor/harbor.yml').read()
            queue = value.get('queue', {})
            rdb = archive.extractfile(self.data.name+'/redis/dump.rdb')
            checksum = hashlib.sha256()
            if rdb is not None:
                for block in iter(lambda: rdb.read(1024 * 1024), b''):
                    checksum.update(block)
            if (not re.fullmatch('[0-9a-f]{64}', str(queue.get('rdb_sha256', ''))) or
                    rdb is None or checksum.hexdigest() != queue['rdb_sha256']):
                raise Refused('portable queue snapshot differs from its stopped source checkpoint')
        if hashlib.sha256(original).hexdigest() != value['source_config_sha256']:
            raise Refused('portable original configuration differs from its stopped checkpoint')
        return value, original

    def restore_logical(self, database, archive):
        """Restore before core starts; never transplant a physical PG directory."""
        remote = '/tmp/kubeauto-registry-' + uuid.uuid4().hex + '.dump'
        self.command([self.docker, 'cp', archive, database+':'+remote], timeout=3600)
        try:
            self.command([self.docker, 'exec', '--user', 'root', database,
                          'chown', 'postgres:postgres', remote])
            self.command([self.docker, 'exec', '--user', 'postgres', database, 'pg_restore',
                '-U', 'postgres', '-d', 'postgres', '--create', '--clean', '--if-exists',
                '--exit-on-error', remote], timeout=3600)
            # --create restores database ACLs, comments and database-specific
            # settings as well as objects. CREATE DATABASE cannot run inside
            # --single-transaction. This target is isolated with core/jobs/proxy
            # stopped; any failure stops it and requires abort-import, never a
            # retry over a partially restored database or a source modification.
        finally:
            self.command([self.docker, 'exec', '--user', 'postgres', database, 'rm', '-f', '--', remote], timeout=30)

    def portable_import(self):
        portable, original = self.portable_input()
        # Old ARM 2.13 must first enter the official AMD64 2.14 bridge.
        # A later same-version 2.16 logical transfer returns it to native ARM.
        versions = ['v2.14.6', 'v2.16.0'] if portable['source'] == 'v2.13.0' else ['v2.16.0']
        for version in versions: self.package(version)
        if self.backup.exists() or self.data.exists() or self.data.is_symlink():
            raise Refused('portable import requires a clean target and new private checkpoint; no blind replay')
        if self.containers(portable['composition']):
            raise Refused('portable import cannot take over an existing Harbor instance')
        needed = (self.transfer/'data.tar').stat().st_size + sum(self.package_paths[v].stat().st_size*4 for v in versions) + 1024**3
        if shutil.disk_usage(self.data.parent).free < needed:
            raise Refused('insufficient target capacity for portable data and official migration')
        # Copy independent TLS identities only after all admission checks.
        for original_path, info in portable['tls'].items():
            destination = real_path(original_path)
            if destination.exists() and digest(destination) != portable['files'][info['file']]:
                raise Refused('portable import will not overwrite an independently changed TLS identity')
        self.backup.mkdir(mode=0o700)
        receipt = {'schema_version': 1, 'kind': 'harbor-portable-target', 'machine': digest('/etc/machine-id'),
            'architecture': self.arch, 'data_directory': str(self.data), 'source': portable['source'],
            'source_recovery': portable['source_recovery'], 'source_machine': portable['source_machine'],
            'portable_sha256': digest(self.transfer/'portable.json'), 'original_configuration': self.config,
            'tls_paths': portable['tls_paths'], 'composition': portable['composition']}
        self.checkpoint(receipt, 'import-intent')
        self.command(['tar', '--acls', '--xattrs', '--same-owner',
            '--exclude='+self.data.name+'/database', '--exclude='+self.data.name+'/harbor',
            '-xpf', self.transfer/'data.tar', '-C', self.data.parent], timeout=3600)
        if (self.data/'database').exists() or self.install.exists():
            raise Refused('physical source database or installer escaped portable exclusions')
        for original_path, info in portable['tls'].items():
            destination = Path(original_path)
            if not destination.exists():
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write((self.transfer/info['file']).read_bytes()); stream.flush(); os.fsync(stream.fileno())
                os.chown(destination, info['uid'], info['gid']); destination.chmod(info['mode'])
        configuration = self.backup/'source-harbor.yml'
        descriptor = os.open(configuration, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream: stream.write(original)
        result = self.run_releases(receipt, versions, portable['composition'], configuration,
                                   portable_dump=self.transfer/'registry.dump')
        result.update(marker='HARBOR_PORTABLE_IMPORT_PASS', source_architecture=portable['architecture'],
                      source_state='original host remains stopped; restore there before any write-authority switch')
        return result

    def abort_import(self):
        """Quarantine only this target; source recovery is a separate host action."""
        if (self.backup.is_symlink() or self.backup.stat().st_uid != os.geteuid() or
                stat.S_IMODE(self.backup.stat().st_mode) != 0o700 or
                self.receipt.is_symlink() or not self.receipt.is_file() or
                self.receipt.stat().st_uid != os.geteuid() or
                stat.S_IMODE(self.receipt.stat().st_mode) != 0o600):
            raise Refused('the private portable target checkpoint is required')
        receipt = json.loads(self.receipt.read_text())
        if (receipt.get('kind') != 'harbor-portable-target' or receipt.get('machine') != digest('/etc/machine-id') or
                receipt.get('architecture') != self.arch or receipt.get('data_directory') != str(self.data)):
            raise Refused('portable cleanup refuses a foreign host or data tree')
        if receipt.get('phase') == 'import-aborted':
            return {'changed': False, 'marker': 'HARBOR_PORTABLE_TARGET_STOPPED'}
        quarantine = self.data.with_name(self.data.name+'.portable-aborted-'+receipt['portable_sha256'][:12])
        if quarantine.exists() or quarantine.is_symlink():
            raise Refused('portable target quarantine already exists; inspect the interrupted cleanup')
        composition = receipt.get('active_composition', receipt['composition'])
        objects = self.containers(composition)
        ids = sorted(o['Id'] for o in objects)
        if ids:
            self.command([self.docker, 'stop', '--time', '60', *ids])
            if sorted(o['Id'] for o in self.containers(composition, running=False)) != ids:
                raise Refused('portable target container identities changed during cleanup')
            self.command([self.docker, 'rm', *ids])
        if self.data.exists(): self.data.rename(quarantine)
        self.checkpoint(receipt, 'import-aborted', quarantine=str(quarantine))
        return {'changed': True, 'marker': 'HARBOR_PORTABLE_TARGET_STOPPED',
                'quarantine': str(quarantine), 'source_recovery': receipt['source_recovery'],
                'boundary': 'target stopped; original source is not restarted by this operation'}

    def backup_source(self):
        if self.backup.exists():
            # A stopped checkpoint precedes any archive output. Revalidate the
            # same machine/configuration/containers, without restarting writers
            # or replacing an existing recovery archive.
            receipt = self.load(stopped=True)
            return self.finish_backup(receipt)
        version = self.api('systeminfo').get('harbor_version', '').split('-')[0]
        if version not in ['v2.13.0', 'v2.14.6', 'v2.16.0']:
            raise Refused('this migration requires an inventoried exact Harbor source release')
        self.health(version)
        composition = self.composition()
        objects = self.containers(composition, running=True)
        # Tar must not cross mounted trees or hide a linked external database.
        for root, dirs, files in os.walk(self.data):
            for name in dirs+files:
                path = Path(root)/name
                if path.is_symlink() or (not path.is_dir() and not path.is_file()) or (path.is_dir() and os.path.ismount(path)):
                    raise Refused('snapshot requires regular local data without nested mounts or links')
        references = {obj['Config']['Image']: obj['Image'] for obj in objects}
        images = sorted(references)
        image_info = json.loads(self.command([self.docker, 'image', 'inspect', *images]))
        if any(item.get('Architecture') != self.arch or item.get('Os') != 'linux' for item in image_info):
            raise Refused('source runtime images do not match the real host platform')
        if len(image_info) != len(images) or any(item['Id'] != references[name] for name,item in zip(images,image_info)):
            raise Refused('a source tag no longer identifies its running image; freeze the original artifact first')
        size = int(self.command(['du', '-sb', self.data]).split()[0])
        needed = size + sum(item['Size'] for item in image_info) + 512*1024*1024
        if shutil.disk_usage(self.backup.parent).free < needed:
            raise Refused('insufficient free space for the complete stopped data and original images')
        self.backup.mkdir(mode=0o700)
        receipt = {'schema_version': 1, 'machine': digest('/etc/machine-id'), 'architecture': self.arch,
            'data_directory': str(self.data), 'source': version, 'source_config_sha256': digest(self.install/'harbor.yml'),
            'configuration_sha256': hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest(),
            'original_configuration': self.config,
            'endpoint': self.endpoint, 'tls_paths': {**{k: str(v) for k,v in self.tls.items()}, 'ca': str(self.ca)},
            'source_container_ids': sorted(o['Id'] for o in objects), 'source_images': references,
            'composition': composition, 'files': {}, 'tls': {}, 'irreversible_point': 'database/schema writes; restore complete stopped backup, never downgrade images'}
        # The composition includes secrets. The receipt stays private on this
        # host and is never returned to Ansible or placed in a Git evidence file.
        self.checkpoint(receipt, 'stop-intent')
        writers = sorted(set(composition['services']) - {'redis', 'postgresql', 'log'})
        self.command([self.compose, '-f', self.install/'docker-compose.yml', 'stop',
                      '--timeout', '120', *writers], cwd=self.install, timeout=180)
        receipt['queue'] = self.save_queue(composition)
        self.command([self.compose, '-f', self.install/'docker-compose.yml', 'stop',
                      '--timeout', '120'], cwd=self.install, timeout=180)
        stopped = self.containers(composition, running=False)
        if sorted(o['Id'] for o in stopped) != receipt['source_container_ids']:
            raise Refused('source container identities changed while stopping')
        self.checkpoint(receipt, 'stopped')
        return self.finish_backup(receipt)

    def finish_backup(self, receipt):
        composition = self.composition()
        tags = self.image_tags()
        if (composition != receipt['composition'] or
                sorted(o['Id'] for o in self.containers(composition, running=False)) != receipt['source_container_ids'] or
                digest(self.install/'harbor.yml') != receipt['source_config_sha256'] or
                any(tags.get(name) != identity for name,identity in receipt['source_images'].items())):
            raise Refused('the stopped backup source changed; retain its checkpoint')
        rdb = self.data/'redis/dump.rdb'
        with rdb.open('rb') as stream:
            header = stream.read(9)
            redis = re.fullmatch(rb'REDIS0[0-9]{3}', header) and 1 <= int(header[6:]) <= 11
            valkey = receipt['queue']['engine'] == 'valkey' and header == b'VALKEY080'
            if not (redis or valkey):
                raise Refused('the stopped queue has no valid RDB snapshot header')
        receipt['queue']['rdb_sha256'] = digest(rdb)
        self.command(['tar', '--acls', '--xattrs', '--numeric-owner', '-cpf', self.backup/'data.tar', '-C', self.data.parent, self.data.name], timeout=3600)
        self.command([self.docker, 'image', 'save', '-o', self.backup/'images.tar', *sorted(receipt['source_images'])], timeout=3600)
        if sorted(o['Id'] for o in self.containers(composition, running=False)) != receipt['source_container_ids']:
            raise Refused('source identities changed during the stopped backup')
        if digest(self.install/'harbor.yml') != receipt['source_config_sha256']:
            raise Refused('source configuration changed during the stopped backup')
        for name in ['data.tar', 'images.tar']:
            (self.backup/name).chmod(0o600)
            receipt['files'][name] = digest(self.backup/name)
        for index, source in enumerate(sorted(set([*self.tls.values(), self.ca]))):
            filename = 'tls-'+str(index)
            shutil.copy2(source, self.backup/filename); (self.backup/filename).chmod(0o600)
            receipt['files'][filename] = digest(self.backup/filename)
            receipt['tls'][str(source)] = {'file': filename, 'mode': stat.S_IMODE(source.stat().st_mode),
                                          'uid': source.stat().st_uid, 'gid': source.stat().st_gid}
        for filename in receipt['files']:
            with (self.backup/filename).open('rb') as stream: os.fsync(stream.fileno())
        self.checkpoint(receipt, 'backup-complete')
        return {'changed': True, 'marker': 'HARBOR_STOPPED_BACKUP_PASS', 'source': receipt['source'],
                'backup': str(self.backup), 'architecture': self.arch}

    def image_tags(self):
        rows = self.command([self.docker, 'image', 'ls', '--no-trunc', '--format', '{{.Repository}}:{{.Tag}} {{.ID}}'])
        return dict(row.rsplit(' ', 1) for row in rows.splitlines() if row)

    def package(self, version):
        """Validate the complete official installer before any runtime mutation."""
        path = self.package_paths.get(version)
        expected = PACKAGES.get(version, {}).get(self.arch)
        if expected is None:
            raise Refused('no official offline bridge exists for this host architecture; physical cross-architecture migration is forbidden')
        if path is None or not path.is_file() or digest(path) != expected:
            raise Refused('the exact official offline installer is missing or corrupt')
        with tarfile.open(path) as archive:
            names = []
            for member in archive:
                parts = Path(member.name).parts
                if (not parts or parts[0] != 'harbor' or Path(member.name).is_absolute() or
                        '..' in parts or not (member.isdir() or member.isfile())):
                    raise Refused('an official installer member escapes its installation directory')
                names.append(member.name)
            required = {'harbor/prepare', 'harbor/install.sh', 'harbor/harbor.'+version+'.tar.gz'}
            if not required <= set(names):
                raise Refused('the official installer is incomplete')
        return path

    def prepare_container(self, version, arguments, mounts, *, privileged=False):
        """Use official prepare arguments, but name and reap our own container."""
        name = 'kubeauto-harbor-prepare-' + uuid.uuid4().hex
        args = [self.docker, 'run', '--name', name, '--network', 'none']
        for source, target in mounts:
            args += ['-v', str(source)+':'+target]
        if privileged:
            args.append('--privileged')
        args += ['goharbor/prepare:'+version, *arguments]
        failure = None
        try:
            return self.command(args, timeout=600)
        except BaseException as error:
            failure = error
            raise
        finally:
            # An interrupted Docker client does not stop its daemon container.
            # Only the unpredictable name allocated by this call is eligible.
            try:
                ids = self.command([self.docker, 'ps', '-aq', '--filter',
                                    'name=^/'+name+'$'], timeout=30).split()
                if len(ids) > 1:
                    raise Refused('prepare-container ownership query is ambiguous')
                if ids:
                    self.command([self.docker, 'rm', '-f', *ids], timeout=30)
            except Exception:
                raise Refused('owned prepare-container cleanup failed; preserve its exact name and private diagnostics') from failure

    def load_release_images(self, version):
        archive_path = self.install/('harbor.'+version+'.tar.gz')
        expected = {}
        with tarfile.open(archive_path) as archive:
            manifest = json.load(archive.extractfile('manifest.json'))
            if not isinstance(manifest, list) or not manifest:
                raise Refused('the official image archive has no Docker image manifest')
            for image in manifest:
                raw = archive.extractfile(image['Config']).read()
                config = json.loads(raw)
                if config.get('architecture') != self.arch or config.get('os') != 'linux':
                    raise Refused('the official image archive differs from the real host platform')
                if not image.get('RepoTags'):
                    raise Refused('an offline image has no exact release tag')
                for tag in image['RepoTags']:
                    if not re.fullmatch(r'goharbor/[a-z0-9-]+:'+re.escape(version), tag):
                        raise Refused('an offline image tag is outside the official release')
                    expected[tag] = 'sha256:'+hashlib.sha256(raw).hexdigest()
        current = self.image_tags()
        if any(tag in current and current[tag] != identity for tag,identity in expected.items()):
            raise Refused('an existing target tag has different bytes; the migration will not overwrite it')
        self.command([self.docker, 'image', 'load', '-i', archive_path], timeout=1800)
        current = self.image_tags()
        if any(current.get(tag) != identity for tag,identity in expected.items()):
            raise Refused('loaded official image identities differ from their offline archive')

    def wait_core(self, composition, version):
        """Core's own healthcheck proves schema migration before proxy/jobs start."""
        deadline = time.monotonic()+900
        while True:
            objects = self.containers(composition)
            cores = [o for o in objects if o['Config']['Labels']['com.docker.compose.service'] == 'core']
            if (len(cores) == 1 and cores[0]['State']['Running'] and
                    cores[0]['State'].get('Health', {}).get('Status') == 'healthy'):
                return
            if any(o['State'].get('Status') in ['dead', 'exited'] for o in objects):
                raise Refused('a migration service exited; retain the stopped recovery point and first-failure diagnostics')
            if time.monotonic() >= deadline:
                raise Refused('Harbor database/schema migration did not reach healthy core within its deadline')
            time.sleep(5)

    def stage_release(self, version, configuration_path, *, with_trivy):
        """Render the exact official installer without starting its services."""
        with tempfile.TemporaryDirectory(prefix='.harbor-unpack-', dir=self.data) as temporary:
            self.command(['tar', '-xzf', self.package_paths[version], '-C', temporary], timeout=600)
            (Path(temporary)/'harbor').rename(self.install)
        shutil.copy2(configuration_path, self.install/'harbor.yml')
        (self.install/'harbor.yml').chmod(0o600)
        self.load_release_images(version)
        self.prepare_container(version, ['migrate', '-i', str(self.install/'harbor.yml')], [(Path('/'), '/hostfs')])
        # The official migration templates preserve the old data path. Its
        # shell wrapper cannot consume quoted scalars; reject rather than
        # silently writing into another directory.
        content = (self.install/'harbor.yml').read_text()
        volumes = re.findall(r'^data_volume:\s*(.*?)\s*$', content, re.MULTILINE)
        if volumes != [str(self.data)]:
            raise Refused('the official migrated configuration changed or quoted the persistent data path')
        inputs = self.install/'input'; inputs.mkdir(mode=0o700)
        shutil.copy2(self.install/'harbor.yml', inputs/'harbor.yml')
        (inputs/'harbor.yml').chmod(0o600)
        options = ['prepare']
        if with_trivy:
            options.append('--with-trivy')
        try:
            self.prepare_container(version, options, [(inputs, '/input'), (self.data, '/data'),
                (self.install, '/compose_location'), (self.install/'common/config', '/config'),
                (Path('/'), '/hostfs/')], privileged=True)
        finally:
            shutil.rmtree(inputs)
        current = self.composition()
        if any(not s.get('image', '').startswith('goharbor/') or not s['image'].endswith(':'+version)
                for s in current['services'].values()):
            raise Refused('generated Compose references an image outside the official target release')
        return current

    def upgrade(self):
        receipt = self.load()
        if receipt['phase'] == 'v2.16.0-ready':
            self.health('v2.16.0')
            return {'changed': False, 'marker': 'HARBOR_OFFICIAL_MIGRATION_PASS', 'target': 'v2.16.0'}
        if receipt['phase'] != 'backup-complete' or receipt['source'] not in ['v2.13.0', 'v2.14.6']:
            raise Refused('upgrade requires a complete stopped source backup; inspect interrupted checkpoints before recovery')
        versions = ['v2.14.6', 'v2.16.0'] if receipt['source'] == 'v2.13.0' else ['v2.16.0']
        packages = {version: self.package(version) for version in versions}
        composition = self.composition()
        stopped = self.containers(composition, running=False)
        if (sorted(o['Id'] for o in stopped) != receipt['source_container_ids'] or
                digest(self.install/'harbor.yml') != receipt['source_config_sha256']):
            raise Refused('the stopped source changed after its backup')
        # Keep the live failed tree plus the original tar and all installer data.
        database_size = int(self.command(['du', '-sb', self.data/'database']).split()[0])
        required_space = sum(path.stat().st_size*4 for path in packages.values()) + database_size*2 + 1024**3
        if shutil.disk_usage(self.data.parent).free < required_space:
            raise Refused('insufficient space for official installer extraction, image loading and database migration')
        self.checkpoint(receipt, 'upgrade-intent', target='v2.16.0', packages={v: digest(p) for v,p in packages.items()})
        return self.run_releases(receipt, versions, composition, self.install/'harbor.yml')

    def run_releases(self, receipt, versions, previous, configuration_path, *, portable_dump=None):
        """Keep external writers and jobs stopped until every migration completes."""
        for version in versions:
            objects = self.containers(previous, running=False)
            if objects:
                self.command([self.docker, 'rm', *[o['Id'] for o in objects]])
            old_install = self.backup/('installer-before-'+version)
            if old_install.exists():
                raise Refused('a prior migration installer exists; no blind overwrite or replay')
            if self.install.exists():
                self.install.rename(old_install)
                configuration_path = old_install/'harbor.yml'
            current = self.stage_release(version, configuration_path,
                with_trivy='trivy-adapter' in previous['services'])
            self.checkpoint(receipt, 'upgrade-intent', active_composition=current, active_version=version)
            try:
                if version == 'v2.16.0' or portable_dump is not None:
                    # The official PG15->18 script refreshes collation metadata but
                    # does not rebuild indexes. Rebuild while core/proxy/jobs are
                    # stopped, so non-ASCII customer metadata has the same safety
                    # boundary as ASCII identifiers.
                    self.command([self.compose, '-f', self.install/'docker-compose.yml', 'up', '-d',
                        '--wait', '--wait-timeout', '900', 'log', 'postgresql'], cwd=self.install, timeout=960)
                    database = current['services']['postgresql']['container_name']
                    if portable_dump is not None:
                        self.restore_logical(database, portable_dump)
                        portable_dump = None
                    if version == 'v2.16.0':
                        self.command([self.docker, 'exec', '--user', 'postgres', database,
                                      'reindexdb', '-a', '-U', 'postgres'], timeout=3600)
                # Start dependencies and core only. Neither external proxy nor
                # jobservice can accept customer writes or run background mutations.
                services = sorted(set(current['services']) - {'proxy', 'jobservice'})
                self.command([self.compose, '-f', self.install/'docker-compose.yml', 'up', '-d', *services],
                             cwd=self.install, timeout=600)
                self.wait_core(current, version)
                if version != versions[-1]:
                    self.command([self.compose, '-f', self.install/'docker-compose.yml', 'stop'], cwd=self.install)
                    self.containers(current, running=False)
                    self.checkpoint(receipt, version+'-ready', external_access='closed', next_version=versions[-1])
                else:
                    self.command([self.compose, '-f', self.install/'docker-compose.yml', 'up', '-d',
                        '--wait', '--wait-timeout', '600'], cwd=self.install, timeout=660)
                    self.health(version)
                    self.checkpoint(receipt, version+'-ready', external_access='open')
            except BaseException:
                # A terminated Docker client does not cancel a daemon exec.
                # Stop this exact version before returning its failed checkpoint.
                owned = self.containers(current)
                if owned:
                    self.command([self.docker, 'stop', '--time', '60', *[o['Id'] for o in owned]], timeout=90)
                raise
            previous = current
        return {'changed': True, 'marker': 'HARBOR_OFFICIAL_MIGRATION_PASS', 'source': receipt['source'],
                'target': 'v2.16.0', 'architecture': self.arch, 'recovery': receipt.get('source_recovery', str(self.backup))}

    def restore(self):
        receipt = self.load()  # All backup bytes are checked before stopping anything.
        if receipt['phase'] == 'restored':
            self.health(receipt['source'])
            return {'changed': False, 'marker': 'HARBOR_ORIGINAL_RESTORE_PASS', 'source': receipt['source']}
        if receipt['phase'].startswith('restore-'):
            raise Refused('an interrupted restore needs its exact checkpoint inspected; no blind replacement')
        for filename in ['data.tar', 'images.tar']:
            if (self.backup/filename).stat().st_size <= 0:
                raise Refused('an original archive is empty')
        with tarfile.open(self.backup/'data.tar') as archive:
            for member in archive:
                path = Path(member.name)
                if (path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != self.data.name or
                        not (member.isdir() or member.isfile() or member.islnk())):
                    raise Refused('a backup archive member escapes the original data tree')
                if member.islnk():
                    link = Path(member.linkname)
                    if link.is_absolute() or '..' in link.parts or not link.parts or link.parts[0] != self.data.name:
                        raise Refused('a backup hard link escapes the original data tree')
        current_tags = self.image_tags()
        if any(name in current_tags and current_tags[name] != expected for name,expected in receipt['source_images'].items()):
            raise Refused('an original source tag was replaced; restore will not overwrite a foreign artifact')
        for original,info in receipt['tls'].items():
            path = real_path(original)
            if path.exists() and digest(path) != receipt['files'][info['file']]:
                raise Refused('TLS identity changed independently; restore will not overwrite it')
        quarantine = self.data.with_name(self.data.name+'.before-restore-'+receipt['source_config_sha256'][:12])
        if quarantine.exists() or quarantine.is_symlink():
            raise Refused('a prior restore quarantine exists; preserve and inspect it')
        if shutil.disk_usage(self.data.parent).free < (self.backup/'data.tar').stat().st_size+256*1024*1024:
            raise Refused('insufficient space to preserve current state and restore original data')
        # A failed prepare may leave an invalid Compose file. Recovery binds
        # the last validated private composition, never the failed live file.
        composition = receipt.get('active_composition', receipt['composition'])
        objects = self.containers(composition)
        self.checkpoint(receipt, 'restore-stop-intent')
        if objects:
            ids = [obj['Id'] for obj in objects]
            self.command([self.docker, 'stop', '--time', '60', *ids])
            if sorted(o['Id'] for o in self.containers(composition, running=False)) != sorted(ids):
                raise Refused('container identities changed while stopping for restore')
            self.command([self.docker, 'rm', *ids])
        self.checkpoint(receipt, 'restore-extract-intent', quarantine=str(quarantine))
        self.data.rename(quarantine)
        self.command(['tar', '--acls', '--xattrs', '--same-owner', '-xpf', self.backup/'data.tar', '-C', self.data.parent], timeout=3600)
        if digest(self.install/'harbor.yml') != receipt['source_config_sha256']:
            raise Refused('restored original configuration differs; preserve quarantine and backup')
        for original,info in receipt['tls'].items():
            path = Path(original)
            if not path.exists():
                with path.open('xb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write((self.backup/info['file']).read_bytes()); stream.flush(); os.fsync(stream.fileno())
                os.chown(path, info['uid'], info['gid']); path.chmod(info['mode'])
        self.command([self.docker, 'image', 'load', '-i', self.backup/'images.tar'], timeout=3600)
        tags = self.image_tags()
        if any(tags.get(name) != checksum for name,checksum in receipt['source_images'].items()):
            raise Refused('restored source image identity differs; do not start the database')
        self.command([self.compose, '-f', self.install/'docker-compose.yml', 'up', '-d', '--wait', '--wait-timeout', '600'], cwd=self.install, timeout=660)
        self.health(receipt['source'])
        self.checkpoint(receipt, 'restored')
        return {'changed': True, 'marker': 'HARBOR_ORIGINAL_RESTORE_PASS', 'source': receipt['source'],
                'quarantine': str(quarantine), 'boundary': 'complete original recovery point; post-backup writes are excluded'}


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(argument_spec={
        'operation': {'type': 'str', 'required': True, 'choices': ['backup', 'upgrade', 'restore', 'export', 'import', 'abort-import']},
        'data_directory': {'type': 'path', 'required': True},
        'backup_directory': {'type': 'path', 'required': True},
        'binary_directory': {'type': 'path', 'required': True},
        'configuration': {'type': 'dict', 'required': True, 'no_log': True},
        'ca_file': {'type': 'path', 'required': True},
        'packages': {'type': 'dict', 'default': {}},
        'transfer_directory': {'type': 'path'},
    }, supports_check_mode=False)
    values = dict(module.params); operation = values.pop('operation')
    try:
        harbor = Harbor(**values)
        with interruptible(), harbor.lease():
            result = {'backup': harbor.backup_source, 'upgrade': harbor.upgrade, 'restore': harbor.restore,
                      'export': harbor.portable_export, 'import': harbor.portable_import,
                      'abort-import': harbor.abort_import}[operation]()
        module.exit_json(**result)
    except Exception as error:
        # Exception text from subprocess/urllib can carry inputs. Only explicit
        # project refusals are safe to expose; originals remain private locally.
        message = str(error) if isinstance(error, Refused) else 'Harbor backup interrupted; inspect the private durable checkpoint and diagnostics'
        module.fail_json(msg=message)


if __name__ == '__main__':
    main()
