"""Exact old-boot service ownership and rejected adoption retirement contracts."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]


def helper(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


ENV = helper('longhorn-v2-post-reboot-environment.py')
ARCHIVE = helper('longhorn-v2-resources-reboot.py').archive_bytes


class PostRebootEnvironment(unittest.TestCase):
    def host(self, machine, boot, active):
        return {'machine_id': machine, 'boot_id': boot, 'dm': {}, 'configs': {'/etc/multipath.conf': 'owned-hash'},
                'units': {n: {'LoadState': 'loaded', 'ActiveState': active, 'UnitFileState': 'enabled'}
                          for n in ('multipathd.service', 'multipathd.socket')}}

    def fixture(self, directory, engine='v2'):
        base = Path(directory); nodes = ENV.POOLS[engine]
        state = {'run_id': 'owned-run', 'cluster_uid': 'owned-cluster', 'host_state': {'nodes': {}},
                 'raw_disks': {n: {'machine_id': 'machine-'+n} for n in nodes}}
        observed, resource_hosts = {}, {}
        for n in nodes:
            state['host_state']['nodes'][n] = {'multipath': {
                'original': self.host('machine-'+n, 'old-'+n, 'active'),
                'current': self.host('machine-'+n, 'old-'+n, 'inactive'), 'phase': 'stopped'}}
            observed[n] = self.host('machine-'+n, 'new-'+n, 'active')
            resource_hosts[n] = {'phase': 'ready', 'current': {'machine_id': 'machine-'+n, 'boot_id': 'new-'+n, 'total': 1024}}
        state_path = base/'state.json'; state_path.write_text(json.dumps(state, indent=3)+'\n')
        lab = SimpleNamespace(DIRECTORY=base, STATE=state_path, NODES=nodes,
            owner=lambda: json.loads(state_path.read_text()), require_lease=MagicMock(), core_identity=MagicMock(),
            write_private=lambda p, data: p.write_text(json.dumps(data)))
        before = {'identity': 'original'}; managers = {'im': {'uid': 'im-uid', 'image': 'fixed/longhorn-instance-manager:v1.12.1'}}
        journal = {'run_id': 'owned-run', 'engine': engine, 'phase': 'setup-intent', 'before': before,
                   'old_managers': managers, 'product_sha256': 'source-sha', 'image_catalog_sha256': 'catalog-sha'}
        adoption_path = base/('longhorn-v2-im-adopt.json'.replace('v2', engine))
        adoption_path.write_text(json.dumps(journal, indent=2)+'\n')
        def digest(path):
            if str(path).endswith('roles/longhorn/files/longhorn.py'): return 'source-sha'
            if str(path).endswith('roles/longhorn/files/images.json'): return 'catalog-sha'
            return hashlib.sha256(path.read_bytes()).hexdigest()
        adopt = SimpleNamespace(RECORD='longhorn-v2-im-adopt.json', SOURCE='roles/longhorn/files/longhorn.py', OLD_TAG='v1.12.1',
            digest=MagicMock(side_effect=digest), qualification=MagicMock(return_value={'accepted': 'fixed'}),
            previous_source=MagicMock(return_value='sha-proven-old-source'), baseline=MagicMock(return_value=before),
            managers=MagicMock(return_value=managers), old_health=MagicMock(), empty=MagicMock())
        mutations = []
        def multipath(lab, node, action, **request):
            if action in ('stop', 'check'):
                self.assertEqual(request['expected'], observed[node])
            if action == 'stop':
                record = ENV.INSTALLED_RECORD if (base/ENV.INSTALLED_RECORD).exists() else ENV.RECORD
                transition = json.loads((base/record).read_text())
                self.assertEqual(transition['hosts'][node]['phase'], 'stop-intent')
                mutations.append(node)
                for unit in observed[node]['units'].values(): unit['ActiveState'] = 'inactive'
            return copy.deepcopy(observed[node])
        install = SimpleNamespace(multipath=MagicMock(side_effect=multipath))
        resources = SimpleNamespace(read_record=MagicMock(return_value={'hosts': resource_hosts}),
                                    probe=lambda lab, node: copy.deepcopy(resource_hosts[node]['current']))
        return lab, install, resources, adopt, observed, mutations, adoption_path

    def installed_fixture(self, directory):
        lab, install, resources, adopt, observed, mutations, path = self.fixture(directory)
        path.unlink()
        state = lab.owner(); state['namespace_uid'] = 'original-namespace'; lab.write_private(lab.STATE, state)
        for name in ('config.yml', 'yml/longhorn/block-disks.json', 'pages.json'):
            file = lab.DIRECTORY/name; file.parent.mkdir(parents=True, exist_ok=True); file.write_text('{}')
        resources.record_path = lambda lab: lab.DIRECTORY/'pages.json'
        install.volume_baseline = MagicMock()
        managers = [{'metadata': {'name': 'im-'+str(i), 'uid': 'manager-'+str(i)},
                     'spec': {'dataEngine': 'v2', 'image': 'fixed:v1.12.1-kubeauto.2'}} for i in range(3)]
        lab.get = lambda kind, name=None, namespace=None: (
            {'items': copy.deepcopy(managers)} if kind == 'instancemanagers.longhorn.io'
            else {'metadata': {'uid': 'pod-'+name}})
        return lab, install, resources, adopt, observed, mutations, managers

    def run_installed_recovery(self, lab, install, resources, adopt):
        with patch.object(ENV, 'consumers', side_effect=lambda lab, node, boot: {'boot': boot}), redirect_stdout(io.StringIO()):
            ENV.recover(lab, install, resources, adopt, ARCHIVE, installed=True)

    def test_installed_current_image_preserves_product_pages_and_original_adoption_history(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, managers = self.installed_fixture(directory)
            historical = lab.DIRECTORY/ENV.RECORD; historical.write_text('{"phase":"complete","historical":true}')
            old = historical.read_bytes(); original = lab.STATE.read_bytes()
            self.run_installed_recovery(lab, install, resources, adopt)
            row = json.loads((lab.DIRECTORY/ENV.INSTALLED_RECORD).read_text())
            self.assertEqual('complete', row['phase']); self.assertEqual(list(lab.NODES), mutations)
            self.assertEqual(original, (lab.DIRECTORY/row['state_archive']).read_bytes())
            self.assertEqual(old, historical.read_bytes()); self.assertEqual('{}', (lab.DIRECTORY/'pages.json').read_text())
            self.assertEqual(2, install.volume_baseline.call_count)
            adopt.old_health.assert_not_called(); adopt.previous_source.assert_not_called()
            self.assertEqual(3, len(row['installation_before']['managers']))

    def test_installed_last_host_or_operation_refusal_prevents_every_mutation(self):
        for fault in ('config', 'holder', 'runtime-intent', 'adoption-intent', 'page-boot', 'symlink'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, install, resources, adopt, observed, mutations, managers = self.installed_fixture(directory)
                if fault == 'config': observed[lab.NODES[-1]]['configs'] = {'foreign': 'changed'}
                elif fault == 'runtime-intent': (lab.DIRECTORY/'longhorn-v2-runtime-resources.json').write_text('{}')
                elif fault == 'adoption-intent': (lab.DIRECTORY/'longhorn-v2-im-adopt.json').write_text('{}')
                elif fault == 'page-boot': resources.read_record.return_value['hosts'][lab.NODES[-1]]['current']['boot_id'] = 'foreign'
                elif fault == 'symlink':
                    (lab.DIRECTORY/'config.yml').unlink(); (lab.DIRECTORY/'config.yml').symlink_to(lab.DIRECTORY/'pages.json')
                def consumers(lab, node, boot):
                    if fault == 'holder' and node == lab.NODES[-1]: raise ValueError('raw disk holder')
                    return {'boot': boot}
                before = lab.STATE.read_bytes()
                with patch.object(ENV, 'consumers', side_effect=consumers), self.assertRaises(ValueError):
                    ENV.recover(lab, install, resources, adopt, ARCHIVE, installed=True)
                self.assertFalse(mutations); self.assertEqual(before, lab.STATE.read_bytes())
                self.assertFalse((lab.DIRECTORY/ENV.INSTALLED_RECORD).exists())

    def test_installed_one_reboot_leaves_other_service_leases_byte_equivalent(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, managers = self.installed_fixture(directory)
            before = lab.owner()
            for node in lab.NODES[1:]:
                observed[node] = copy.deepcopy(before['host_state']['nodes'][node]['multipath']['current'])
                resources.read_record.return_value['hosts'][node]['current']['boot_id'] = 'old-'+node
            self.run_installed_recovery(lab, install, resources, adopt)
            self.assertEqual(mutations, [lab.NODES[0]])
            after = lab.owner()
            for node in lab.NODES[1:]:
                self.assertEqual(after['host_state']['nodes'][node], before['host_state']['nodes'][node])

    def test_unrebooted_foreign_service_state_blocks_all_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, managers = self.installed_fixture(directory)
            node = lab.NODES[-1]
            observed[node]['boot_id'] = 'old-'+node
            resources.read_record.return_value['hosts'][node]['current']['boot_id'] = 'old-'+node
            with self.assertRaisesRegex(ValueError, 'unrebooted'):
                self.run_installed_recovery(lab, install, resources, adopt)
            self.assertFalse(mutations)

    def test_no_reboot_refuses_without_publishing_recovery_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, managers = self.installed_fixture(directory)
            before = lab.owner()
            for node in lab.NODES:
                observed[node] = copy.deepcopy(before['host_state']['nodes'][node]['multipath']['current'])
                resources.read_record.return_value['hosts'][node]['current']['boot_id'] = 'old-'+node
            with self.assertRaisesRegex(ValueError, 'at least one changed boot'):
                self.run_installed_recovery(lab, install, resources, adopt)
            self.assertFalse(mutations)
            self.assertFalse((lab.DIRECTORY/ENV.INSTALLED_RECORD).exists())

    def test_installed_lost_stop_response_retains_intent_and_refuses_implicit_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, managers = self.installed_fixture(directory)
            original = install.multipath.side_effect; state = lab.STATE.read_bytes()
            def lose(lab, node, action, **request):
                value = original(lab, node, action, **request)
                if action == 'stop': raise TimeoutError('lost stop reply')
                return value
            install.multipath.side_effect = lose
            with self.assertRaises(TimeoutError): self.run_installed_recovery(lab, install, resources, adopt)
            row = json.loads((lab.DIRECTORY/ENV.INSTALLED_RECORD).read_text())
            self.assertEqual('stop-intent', row['hosts'][lab.NODES[0]]['phase'])
            self.assertEqual(state, lab.STATE.read_bytes())
            with self.assertRaisesRegex(ValueError, 'previous environment recovery intent'):
                self.run_installed_recovery(lab, install, resources, adopt)

    def test_installed_manager_or_input_change_prevents_success(self):
        for fault in ('manager', 'config'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, install, resources, adopt, observed, mutations, managers = self.installed_fixture(directory)
                def baseline(lab):
                    if install.volume_baseline.call_count == 2:
                        if fault == 'manager': managers[0]['metadata']['uid'] = 'foreign'
                        else: (lab.DIRECTORY/'config.yml').write_text('{"changed":true}')
                install.volume_baseline.side_effect = baseline
                with self.assertRaisesRegex(ValueError, 'installation changed'):
                    self.run_installed_recovery(lab, install, resources, adopt)
                row = json.loads((lab.DIRECTORY/ENV.INSTALLED_RECORD).read_text())
                self.assertEqual('state-update-intent', row['phase'])

    def test_fixed_entry_selects_current_installation_only_without_an_adoption_intent(self):
        for pending in (False, True):
            with self.subTest(pending=pending), tempfile.TemporaryDirectory() as directory:
                lab, install, resources, adopt, observed, mutations, managers = self.installed_fixture(directory)
                if pending: (lab.DIRECTORY/adopt.RECORD).write_text('{}')
                modules = {'longhorn-v2-install.py': SimpleNamespace(fixture=lambda: (None, lab)),
                           'longhorn-v2-resources.py': resources, 'longhorn-v2-im-adopt.py': adopt,
                           'longhorn-v2-resources-reboot.py': SimpleNamespace(archive_bytes=ARCHIVE)}
                with patch.object(ENV, 'load', side_effect=lambda name: modules[name]), \
                        patch.object(ENV, 'recover') as recovery, \
                        patch.object(sys, 'argv', ['recovery', 'v2-post-reboot-environment-recovery']):
                    ENV.main()
                self.assertEqual(not pending, recovery.call_args.kwargs['installed'])

    def run_recovery(self, lab, install, resources, adopt, *, engine='v2'):
        def consumers(lab, node, boot): return {'machine_id': 'machine-'+node, 'boot_id': boot, 'holders': [], 'iscsi_sessions': 0}
        with patch.object(ENV, 'consumers', side_effect=consumers), redirect_stdout(io.StringIO()):
            ENV.recover(lab, install, resources, adopt, ARCHIVE, engine=engine)

    def test_current_boot_service_stop_and_byte_exact_state_adoption_archives(self):
        for engine in ENV.POOLS:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory:
                lab, install, resources, adopt, observed, mutations, path = self.fixture(directory, engine)
                state_raw = lab.STATE.read_bytes(); adoption_raw = path.read_bytes()
                self.run_recovery(lab, install, resources, adopt, engine=engine)
                transition = json.loads((lab.DIRECTORY/ENV.RECORD).read_text())
                self.assertEqual(transition['phase'], 'complete'); self.assertFalse(path.exists())
                self.assertEqual((lab.DIRECTORY/transition['state_archive']).read_bytes(), state_raw)
                self.assertEqual((lab.DIRECTORY/transition['adoption_archive']).read_bytes(), adoption_raw)
                self.assertEqual(mutations, list(ENV.POOLS[engine]))
                for n, host in lab.owner()['host_state']['nodes'].items():
                    entry = host['multipath']
                    self.assertEqual(entry['original']['boot_id'], 'new-'+n)
                    self.assertEqual(entry['original']['units']['multipathd.service']['ActiveState'], 'active')
                    self.assertEqual(entry['current']['units']['multipathd.service']['ActiveState'], 'inactive')
                self.assertEqual(adopt.old_health.call_count, 2)
                adopt.old_health.assert_called_with(lab, 'sha-proven-old-source')

    def test_last_host_refusal_prevents_every_remote_stop(self):
        for key, value in (('boot_id', 'same'), ('machine_id', 'foreign'), ('dm', {'dm-1': 'mpath-foreign'}),
                           ('configs', {'/etc/multipath.conf': 'foreign'})):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                lab, install, resources, adopt, observed, mutations, path = self.fixture(directory)
                last = lab.NODES[-1]
                observed[last][key] = 'old-'+last if value == 'same' else value
                original = lab.STATE.read_bytes()
                with self.assertRaises(ValueError): self.run_recovery(lab, install, resources, adopt)
                self.assertEqual(mutations, []); self.assertEqual(lab.STATE.read_bytes(), original)
                self.assertTrue(path.exists()); self.assertFalse((lab.DIRECTORY/ENV.RECORD).exists())

    def test_originally_inactive_service_cannot_be_silently_stopped_after_foreign_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, path = self.fixture(directory)
            state = lab.owner(); state['host_state']['nodes'][lab.NODES[-1]]['multipath']['original']['units']['multipathd.service']['ActiveState'] = 'inactive'
            lab.write_private(lab.STATE, state)
            with self.assertRaisesRegex(ValueError, 'foreign service'): self.run_recovery(lab, install, resources, adopt)
            self.assertEqual(mutations, [])

    def test_changed_enable_state_and_old_page_boot_are_refused(self):
        for fault in ('enable', 'resource-boot', 'pages'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, install, resources, adopt, observed, mutations, path = self.fixture(directory)
                n = lab.NODES[-1]
                if fault == 'enable': observed[n]['units']['multipathd.socket']['UnitFileState'] = 'disabled'
                elif fault == 'resource-boot': resources.read_record.return_value['hosts'][n]['current']['boot_id'] = 'old-'+n
                else: resources.read_record.return_value['hosts'][n]['current']['total'] = 0
                with self.assertRaises(ValueError): self.run_recovery(lab, install, resources, adopt)
                self.assertEqual(mutations, [])

    def test_only_unchanged_setup_intent_can_retire(self):
        for fault in ('phase', 'new-managers', 'source', 'catalog', 'input', 'manager', 'unhealthy', 'request'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, install, resources, adopt, observed, mutations, path = self.fixture(directory)
                row = json.loads(path.read_text())
                if fault == 'phase': row['phase'] = 'business-intent'
                elif fault == 'new-managers': row['new_managers'] = {}
                elif fault == 'source': row['product_sha256'] = 'foreign'
                elif fault == 'catalog': row['image_catalog_sha256'] = 'foreign'
                elif fault == 'input': adopt.baseline.return_value = {'changed': True}
                elif fault == 'manager': adopt.managers.return_value = {'replacement': {}}
                elif fault == 'unhealthy': adopt.old_health.side_effect = ValueError('unhealthy')
                else: (lab.DIRECTORY/'longhorn-request-input.json').write_text('{}')
                path.write_text(json.dumps(row)); raw = path.read_bytes()
                with self.assertRaises(ValueError): self.run_recovery(lab, install, resources, adopt)
                self.assertEqual(path.read_bytes(), raw); self.assertEqual(mutations, [])

    def test_service_stop_lost_response_keeps_intent_and_original_adoption_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, path = self.fixture(directory)
            original = install.multipath.side_effect; state_raw = lab.STATE.read_bytes(); adoption_raw = path.read_bytes()
            def lose(lab, n, action, **request):
                result = original(lab, n, action, **request)
                if action == 'stop': raise TimeoutError('SSH response lost')
                return result
            install.multipath.side_effect = lose
            with self.assertRaises(TimeoutError): self.run_recovery(lab, install, resources, adopt)
            transition = json.loads((lab.DIRECTORY/ENV.RECORD).read_text())
            self.assertEqual(transition['hosts'][lab.NODES[0]]['phase'], 'stop-intent')
            self.assertEqual(lab.STATE.read_bytes(), state_raw); self.assertEqual(path.read_bytes(), adoption_raw)
            with self.assertRaisesRegex(ValueError, 'previous environment recovery intent'):
                self.run_recovery(lab, install, resources, adopt)

    def test_post_stop_product_change_prevents_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, path = self.fixture(directory)
            adopt.managers.side_effect = [copy.deepcopy(adopt.managers.return_value), {'new-manager': {}}]
            with self.assertRaisesRegex(ValueError, 'manager identity or image'):
                self.run_recovery(lab, install, resources, adopt)
            self.assertTrue(path.exists())
            self.assertEqual(json.loads((lab.DIRECTORY/ENV.RECORD).read_text())['phase'], 'state-update-intent')

    def test_consumer_refusal_prevents_stop_and_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, install, resources, adopt, observed, mutations, path = self.fixture(directory)
            with patch.object(ENV, 'consumers', side_effect=ValueError('raw disk holder')):
                with self.assertRaisesRegex(ValueError, 'holder'):
                    ENV.recover(lab, install, resources, adopt, ARCHIVE)
            self.assertEqual(mutations, []); self.assertEqual(list(lab.DIRECTORY.glob('*pre-reboot*')), [])

    def test_remote_probe_program_compiles_and_has_no_mutation_command(self):
        code = ENV.consumers_script(); compile(code, '<consumer-probe>', 'exec')
        for required in ('iscsi_session', 'iscsi_connection', 'holders', 'WWN,SERIAL', "startswith('mpath-')"):
            self.assertIn(required, code)
        for forbidden in ("'stop'", "'start'", "'disable'", 'write_text', 'unlink'):
            self.assertNotIn(forbidden, code)

    def test_absent_service_stays_absent_under_the_narrow_admission_contract(self):
        # Fixed canonical Debian/Ubuntu absence semantics: no unit cannot be
        # turned into an invented active service by recovery.
        original = self.host('machine', 'old', 'inactive')
        for u in original['units'].values(): u.update(LoadState='not-found', UnitFileState='')
        entry = {'phase': 'stopped', 'original': original, 'current': copy.deepcopy(original)}
        current = copy.deepcopy(original); current['boot_id'] = 'new'
        ENV.admission(entry, current, 'machine', {'phase': 'ready', 'current': {
            'machine_id': 'machine', 'boot_id': 'new', 'total': 1024}})

    def test_shell_modes_preserve_first_exit_and_never_emit_pass_on_failure(self):
        for mode in ENV.MODES:
            for fail in (True, False):
                with self.subTest(mode=mode, fail=fail), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory); (base/'.venv/bin').mkdir(parents=True)
                    os.symlink(sys.executable, base/'.venv/bin/python')
                    (base/'tests/helpers').mkdir(parents=True); (base/'kube-bin').mkdir()
                    for name in ('longhorn-v2-core.py', 'longhorn-v2-dr-target.py'):
                        (base/'tests/helpers'/name).write_text('pass\n')
                    (base/'tests/helpers/longhorn-v2-post-reboot-environment.py').write_text(
                        'import sys\nsys.exit('+('17' if fail else '0')+')\n')
                    kubectl = base/'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                    shell = (ROOT/'tests/helpers/longhorn-v2-post-reboot-environment.sh').read_text()
                    script = base/'run.sh'; script.write_text(shell.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)))
                    result = subprocess.run(['bash', str(script), mode], text=True, capture_output=True, timeout=10,
                                            env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes'))
                    marker = 'LONGHORN_'+mode.upper().replace('-', '_')
                    durable = next(line.split('path=', 1)[1] for line in result.stdout.splitlines()
                                   if line.startswith(marker+'_DURABLE_DIRECTORY'))
                    try:
                        self.assertEqual(result.returncode, 17 if fail else 0)
                        self.assertEqual((Path(durable)/'exit').read_text().strip(), str(result.returncode))
                        if fail:
                            self.assertNotIn(marker+'_PASS', result.stdout)
                            self.assertIn('LONGHORN_FIRST_FAILURE', result.stderr)
                        else: self.assertIn(marker+'_PASS', result.stdout)
                    finally: shutil.rmtree(durable)


if __name__ == '__main__': unittest.main()
