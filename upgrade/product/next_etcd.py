"""Bind the serial etcd patch playbook to private, immutable live identities."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess

from upgrade.product.next_platform import snapshot
from upgrade.product.kubernetes_preflight import Refused
from upgrade.product.hop import PINS_PATH


class EtcdPatch:
    steps = ['platform/etcd-v3.6.16']

    def __init__(self, manager, cluster, variables, source, state):
        self.manager, self.cluster, self.state = manager, cluster, state
        self.retained = (source.get('source'), source.get('target')) == ('v1.35.10', 'v1.36.6')
        self.variables = dict(variables)
        inventory = source['effective_inventory']
        self.hosts = inventory['groups']['etcd']
        if (len(self.hosts) not in [1, 3, 5] or
                len(set(self.hosts)) != len(self.hosts)):
            raise Refused('unique supported etcd member inventory required')
        self.variables.update(inventory['hosts'][self.hosts[0]]['parameters'])
        ssl = manager.clusters_dir / cluster / 'ssl'
        self.endpoints = ['https://' + host + ':2379' for host in self.hosts]
        self.binary = manager.extra_bin_dir / 'etcdctl'
        pins = json.loads(PINS_PATH.read_text())
        arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
        if arch not in pins['etcd']:
            raise Refused('etcd architecture is outside the admitted pack')
        self.pins = pins['etcd'][arch]
        for name in ['etcd', 'etcdctl']:
            path = manager.extra_bin_dir / name
            if (path.is_symlink() or not path.is_file() or
                    not os.access(path, os.X_OK) or
                    hashlib.sha256(path.read_bytes()).hexdigest() != self.pins[name]):
                raise Refused('etcd patch input differs from the admitted official bytes')
        self.argv = [str(self.binary), '--endpoints=' + ','.join(self.endpoints),
            '--cacert=' + str(ssl / 'ca.pem'), '--cert=' + str(ssl / 'etcd.pem'),
            '--key=' + str(ssl / 'etcd-key.pem'), '--dial-timeout=5s', '--command-timeout=15s']
        self.boundary = snapshot(state / 'etcd-source.json', self.observe)

    def read(self, *args):
        try:
            result = subprocess.run([*self.argv, *args, '--write-out=json'],
                capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise Refused('etcd TLS observation failed; retain private diagnostics')
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            if isinstance(error, Refused):
                raise
            raise Refused('etcd TLS observation was unavailable or invalid') from None

    def observe(self):
        members = self.read('member', 'list')['members']
        statuses = self.read('endpoint', 'status')
        health = self.read('endpoint', 'health')
        ids = [member['ID'] for member in members]
        if (len(members) != len(self.hosts) or len(set(ids)) != len(ids) or
                any(member.get('isLearner', False) for member in members) or
                sorted(url for member in members for url in member['clientURLs']) != sorted(self.endpoints) or
                len(statuses) != len(self.hosts) or
                sorted(row['Endpoint'] for row in statuses) != sorted(self.endpoints) or
                len(health) != len(self.hosts) or
                sorted(row['endpoint'] for row in health) != sorted(self.endpoints) or
                any(row.get('health') is not True for row in health) or
                len({row['Status']['header']['cluster_id'] for row in statuses}) != 1):
            raise Refused('healthy exact etcd membership and endpoint coverage required')
        for row in statuses:
            status = row['Status']
            member = next(member for member in members if row['Endpoint'] in member['clientURLs'])
            if (status['version'] not in (['3.6.16'] if self.retained else ['3.6.4', '3.6.16']) or
                    status['header']['member_id'] != member['ID'] or
                    type(status['header']['cluster_id']) is not int or status['header']['cluster_id'] <= 0 or
                    type(status['header']['revision']) is not int or status['header']['revision'] <= 0 or
                    status.get('errors') or status.get('isLearner', False) or status.get('leader') not in ids):
                raise Refused('etcd source profile differs from the reviewed patch edge')
        units = {}
        for host in self.hosts:
            path = self.state / ('etcd-unit-' + host + '.json')
            if (path.is_symlink() or not path.is_file() or
                    path.stat().st_mode & 0o777 != 0o600 or path.stat().st_uid != os.geteuid()):
                raise Refused('private initial etcd unit identity required')
            record = json.loads(path.read_text())
            if record['host'] != host or not re.fullmatch('[a-f0-9]{64}', record.get('sha256', '')):
                raise Refused('etcd unit identity is invalid or belongs to another host')
            units[host] = record['sha256']
        return {'members': members, 'cluster_id': statuses[0]['Status']['header']['cluster_id'],
                'revision': min(row['Status']['header']['revision'] for row in statuses), 'units': units}

    def run(self, journal):
        step = self.steps[0]
        completed = step in journal.data['completed']
        resume = completed or journal.data['active'] == step
        values = {**self.variables, 'UPGRADE_ETCD_RECOVERY_VERIFIED': True,
            'UPGRADE_ETCD_RESUME': resume or all(row['Status']['version'] == '3.6.16'
                for row in self.read('endpoint', 'status')),
            'UPGRADE_ETCD_VERIFY_ONLY': completed or self.retained,
            'UPGRADE_ETCD_CLUSTER_ID': self.boundary['cluster_id'],
            'UPGRADE_ETCD_SOURCE_REVISION': self.boundary['revision'],
            'UPGRADE_ETCD_MEMBERS': self.boundary['members'],
            'UPGRADE_ETCD_ENDPOINTS': self.endpoints, 'UPGRADE_ETCD_UNIT_SHA256': self.boundary['units'],
            'UPGRADE_ETCDCTL': str(self.binary), 'UPGRADE_ETCDCTL_SHA256': self.pins['etcdctl'],
            'UPGRADE_ETCD_TARGETS': {host: {'path': str(self.manager.extra_bin_dir / 'etcd'),
                'sha256': self.pins['etcd']} for host in self.hosts}}
        if not completed:
            journal.begin(step)
        try:
            # Accepted boundaries still execute the real playbook's full TLS,
            # process, unit and disk checks. Verify-only forbids copy/restart.
            result = self.manager._run_playbook(self.cluster, Path(__file__).with_name('etcd.yml'),
                                                extra_vars=values)
            if type(result.rc) is not int or result.rc != 0:
                raise Refused('etcd patch playbook refused')
        except BaseException as error:
            if not completed:
                journal.failed(step, getattr(error, 'rc', 1), 'kubernetes')
            raise
        if not completed:
            journal.complete(step)
