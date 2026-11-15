"""Internal Cilium stage for the admitted config + kubecli upgrade path.

The outer product runner must own the cluster journal, source identity, leases,
backups and artifact receipts. This module has no standalone CLI or admission
shortcut. A successful stage is only Cilium workload convergence, not GA or a
continuous-traffic result. Failed main upgrades are preserved for diagnosis.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import yaml

from upgrade.product.cilium import (CHARTS, CiliumRefused, at, prepare_values,
                                    validate_preflight, validate_removed_policy_rules)


PREFLIGHT_OBJECTS = ['daemonset/cilium-pre-flight-check',
                     'deployment/cilium-pre-flight-check', 'serviceaccount/cilium-pre-flight',
                     'clusterrole/cilium-pre-flight', 'clusterrolebinding/cilium-pre-flight']
OWNER_LABEL = 'kubeauto.io/upgrade-owner'


class CommandFailed(CiliumRefused):
    def __init__(self, tool, rc, argv):
        self.rc = rc
        self.argv = tuple(argv)
        super().__init__(f'{tool} stage command failed (rc={rc}); retain private runner diagnostics')


class CiliumStage:
    def __init__(self, helm, kubectl, kubeconfig, chart, run_id, *, timeout=600, progress=None):
        if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}', run_id) or
                type(timeout) is not int or not 30 <= timeout <= 900):
            raise CiliumRefused('valid run identity and bounded stage timeout are required')
        self.helm = str(Path(helm).resolve())
        self.kubectl = str(Path(kubectl).resolve())
        self.kubeconfig = str(Path(kubeconfig).resolve())
        self.chart = Path(chart).resolve()
        self.owner = hashlib.sha256(run_id.encode()).hexdigest()[:32]
        self.release = 'kubeauto-cilium-preflight-' + self.owner[:16]
        self.timeout = timeout
        self.progress = progress or (lambda text: print(text, flush=True))

    def command(self, tool, arguments):
        binary = self.helm if tool == 'helm' else self.kubectl
        args = [binary, '--kubeconfig', self.kubeconfig, '--namespace', 'kube-system', *arguments]
        if tool == 'kubectl':
            args.extend(['--request-timeout=10s', '--insecure-skip-tls-verify=false'])
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60,
                                    env={**os.environ, 'HELM_DRIVER': 'secret'})
        except (OSError, subprocess.TimeoutExpired):
            raise CommandFailed(tool, 124, args) from None
        if result.returncode:
            # Neither Helm values, Secret data nor exec-plugin stderr is public.
            raise CommandFailed(tool, result.returncode, args)
        return result.stdout

    def read(self, tool, args):
        try:
            value = json.loads(self.command(tool, [*args, '-o', 'json']))
        except ValueError as error:
            if isinstance(error, CiliumRefused):
                raise
            raise CiliumRefused('stage read returned invalid JSON') from None
        if not isinstance(value, dict):
            raise CiliumRefused('stage read must return an object')
        return value

    def objects(self):
        return self.read('kubectl', ['get', *PREFLIGHT_OBJECTS, '--ignore-not-found'])

    def records(self):
        return self.read('kubectl', ['get', 'secrets', '-l', 'owner=helm,name=' + self.release])

    @staticmethod
    def items(value):
        if not isinstance(value.get('items'), list):
            raise CiliumRefused('API list is incomplete')
        return value['items']

    def cleanup_preflight(self):
        records = self.items(self.records())
        if not records:
            if self.items(self.objects()):
                raise CiliumRefused('preflight objects exist without their owned Helm record; retain for diagnosis')
            return
        # Label names contain dots: do not traverse them as a dotted value path.
        if any(record.get('metadata', {}).get('labels', {}).get(OWNER_LABEL) != self.owner
               or record.get('type') != 'helm.sh/release.v1' for record in records):
            raise CiliumRefused('foreign preflight release record; cleanup refused')
        for obj in self.items(self.objects()):
            annotations = at(obj, 'metadata.annotations', {})
            if (annotations.get('meta.helm.sh/release-name') != self.release or
                    annotations.get('meta.helm.sh/release-namespace') != 'kube-system'):
                raise CiliumRefused('foreign preflight resource; cleanup refused')
        self.command('helm', ['uninstall', self.release, '--wait', '--timeout', '30s'])
        if self.items(self.objects()) or self.items(self.records()):
            raise CiliumRefused('preflight cleanup did not converge')
        self.progress('UPGRADE_CILIUM_PREFLIGHT_CLEAN_PASS')

    def wait_preflight(self):
        deadline = time.monotonic() + self.timeout
        while True:
            agent = self.read('kubectl', ['get', 'daemonset', 'cilium'])
            preflight = self.read('kubectl', ['get', 'daemonset', 'cilium-pre-flight-check'])
            validator = self.read('kubectl', ['get', 'deployment', 'cilium-pre-flight-check'])
            agent_pods = self.read('kubectl', ['get', 'pods', '-l', 'k8s-app=cilium'])
            preflight_pods = self.read('kubectl', ['get', 'pods', '-l', 'k8s-app=cilium-pre-flight-check'])
            try:
                validate_preflight(agent, preflight, validator, agent_pods, preflight_pods)
                return
            except CiliumRefused:
                if time.monotonic() >= deadline:
                    raise CiliumRefused('official preflight did not converge within its fixed window') from None
            self.progress('Cilium preflight: waiting for per-node image readiness and policy validator 1/1')
            time.sleep(5)

    def workloads(self, values, version):
        definitions = [('daemonset', 'cilium', {'cilium-agent': 'cilium:v' + version}),
                       ('deployment', 'cilium-operator', {'cilium-operator': 'cilium-operator-generic:v' + version})]
        if at(values, 'hubble.relay.enabled', False):
            definitions.append(('deployment', 'hubble-relay', {'hubble-relay': 'hubble-relay:v' + version}))
        if at(values, 'hubble.ui.enabled', False):
            ui_tag = at(values, 'hubble.ui.backend.image.tag')
            definitions.append(('deployment', 'hubble-ui',
                {'frontend': 'hubble-ui:' + ui_tag, 'backend': 'hubble-ui-backend:' + ui_tag}))
        observed = {}
        for kind, name, images in definitions:
            obj = self.read('kubectl', ['get', kind, name])
            if (at(obj, 'metadata.name') != name or at(obj, 'metadata.namespace') != 'kube-system' or
                    not at(obj, 'metadata.uid')):
                raise CiliumRefused('Cilium workload identity differs')
            annotations = at(obj, 'metadata.annotations', {})
            if (annotations.get('meta.helm.sh/release-name') != 'cilium' or
                    annotations.get('meta.helm.sh/release-namespace') != 'kube-system'):
                raise CiliumRefused('Cilium workloads must retain their original Helm owner')
            containers = at(obj, 'spec.template.spec.containers', [])
            actual = {c['name']: c['image'] for c in containers}
            expected = {key: 'registry.talkschool.cn:5000/brinnatt/' + value for key, value in images.items()}
            if actual != expected:
                raise CiliumRefused('Cilium workload images differ from the reviewed version')
            observed[kind + '/' + name] = obj['metadata']['uid']
        return observed

    def wait_workloads(self, identities):
        deadline = time.monotonic() + self.timeout
        while True:
            pending = []
            for workload, uid in identities.items():
                obj = self.read('kubectl', ['get', workload])
                if at(obj, 'metadata.uid') != uid:
                    raise CiliumRefused('workload ownership changed during convergence')
                status = obj.get('status', {})
                observed = status.get('observedGeneration', -1) >= at(obj, 'metadata.generation', 0)
                if workload.startswith('daemonset/'):
                    desired = status.get('desiredNumberScheduled', 0)
                    keys = ['currentNumberScheduled', 'updatedNumberScheduled', 'numberReady', 'numberAvailable']
                    healthy = (type(desired) is int and desired > 0 and
                        status.get('numberMisscheduled', 0) == 0 and
                        all(status.get(key) == desired for key in keys))
                else:
                    desired = at(obj, 'spec.replicas', 1)
                    keys = ['replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas']
                    healthy = (type(desired) is int and desired > 0 and
                        all(status.get(key) == desired for key in keys))
                if not (healthy and observed):
                    pending.append(workload)
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise CiliumRefused('Cilium workload convergence exceeded its fixed window')
            self.progress('Cilium: waiting for ' + ', '.join(pending))
            time.sleep(5)

    def run(self, current_reviewed, source, target):
        if target not in CHARTS or hashlib.sha256(self.chart.read_bytes()).hexdigest() != CHARTS[target]:
            raise CiliumRefused('Cilium Chart does not match the frozen official digest')
        metadata = self.read('helm', ['get', 'metadata', 'cilium'])
        if (metadata.get('name') != 'cilium' or metadata.get('namespace') != 'kube-system' or
                metadata.get('chart') != 'cilium' or metadata.get('version') != source or
                metadata.get('status') != 'deployed' or metadata.get('appVersion') != source):
            raise CiliumRefused('observed Helm release is not the exact healthy source edge')
        current = self.read('helm', ['get', 'values', 'cilium'])
        values = prepare_values(current, current_reviewed, source, target)
        preflight_values = prepare_values(current, current_reviewed, source, target, preflight=True)
        policies = [self.read('kubectl', ['get', name, '--all-namespaces']) for name in
                    ['ciliumnetworkpolicies.cilium.io', 'ciliumclusterwidenetworkpolicies.cilium.io']]
        validate_removed_policy_rules(policies)
        # Catch foreign or stale static preflight resources before creating any.
        if self.items(self.objects()) or self.items(self.records()):
            raise CiliumRefused('preflight resources/record already exist; diagnose their owner before retry')
        identities = self.workloads(current, source)
        self.wait_workloads(identities)
        with tempfile.TemporaryDirectory(prefix='kubeauto-cilium-') as directory:
            directory = Path(directory)
            paths = []
            for name, data in [('preflight', preflight_values), ('target', values)]:
                path = directory / (name + '.yaml')
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    yaml.safe_dump(data, stream)
                paths.append(str(path))
            try:
                self.command('helm', ['install', self.release, str(self.chart), '-f', paths[0],
                    '--labels', OWNER_LABEL + '=' + self.owner, '--timeout', str(self.timeout) + 's'])
                self.wait_preflight()
            except BaseException as failure:
                try:
                    self.cleanup_preflight()
                except CiliumRefused as cleanup_error:
                    failure.add_note(str(cleanup_error))
                raise
            self.cleanup_preflight()
            # Re-read source inputs after preflight; reject concurrent Helm edits.
            if (self.read('helm', ['get', 'metadata', 'cilium']) != metadata or
                    self.read('helm', ['get', 'values', 'cilium']) != current or
                    self.workloads(current, source) != identities):
                raise CiliumRefused('source changed during preflight; no main upgrade permitted')
            self.command('helm', ['upgrade', 'cilium', str(self.chart), '--reset-values',
                '-f', paths[1], '--timeout', str(self.timeout) + 's'])
            self.wait_workloads(identities)
            if self.workloads(values, target) != identities:
                raise CiliumRefused('Cilium workload replacement changed an owned UID')
            after = self.read('helm', ['get', 'metadata', 'cilium'])
            if (after.get('name') != 'cilium' or after.get('namespace') != 'kube-system' or
                    after.get('chart') != 'cilium' or after.get('appVersion') != target or
                    after.get('version') != target or after.get('status') != 'deployed' or
                    after.get('revision') != metadata['revision'] + 1 or
                    self.read('helm', ['get', 'values', 'cilium']) != values):
                raise CiliumRefused('target Helm revision or user values differ')
        self.progress('UPGRADE_CILIUM_STAGE_PASS')
        return {'source': source, 'target': target, 'source_revision': metadata['revision'],
                'target_revision': after['revision'], 'workload_uids': deepcopy(identities)}
