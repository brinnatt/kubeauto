"""Reboot page-pool recovery ownership, intent and old-health contracts."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]


def helper(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REC = helper('longhorn-v2-resources-reboot.py')
RES = helper('longhorn-v2-resources.py')


class RebootRecovery(unittest.TestCase):
    def setUp(self):
        base = patch.object(REC, 'BASE', ROOT)
        base.start()
        self.addCleanup(base.stop)

    def fixture(self, directory, nodes=REC.NODES):
        state = {'run_id': 'owned-run', 'cluster_uid': 'owned-cluster', 'raw_disks': {
            n: {'machine_id': 'machine-'+n, 'path': '/dev/disk/by-id/wwn-owned',
                'identity': 'wwn-owned', 'size_bytes': 100*1024**3} for n in nodes}}
        def host(node, boot, pages):
            return dict(machine_id='machine-'+node, boot_id=boot, total=pages, free=pages,
                        reserved=0, available_kib=6*1024**2, kubelet_active='active')
        record = {'id': '54a8e3e1-2226-4545-b4a9-62b40830280d', 'run_id': state['run_id'],
                  'cluster_uid': state['cluster_uid'], 'hosts': {
                      n: {'original': host(n, 'old-'+n, 0), 'current': host(n, 'old-'+n, 1024),
                          'phase': 'ready'} for n in nodes}}
        lab = SimpleNamespace(DIRECTORY=Path(directory), NODES=nodes,
                              owner=lambda: state, require_lease=MagicMock(), core_identity=MagicMock(), installation_health=MagicMock(),
                              inputs_current=MagicMock(return_value=True))
        lab.write_private = lambda p, r: p.write_text(json.dumps(r))
        lab.get = MagicMock(return_value={'items': [{'spec': {'dataEngine': 'v2'}, 'status': {}}]})
        lab.wait = lambda name, timeout, callback: self.assertTrue(callback())
        path = RES.record_path(lab)
        path.write_text(json.dumps(record, indent=3)+'\n')
        adopt = SimpleNamespace(qualification=MagicMock(return_value={'status': 'pass'}),
            previous_source=MagicMock(return_value='sha-proven-old-product-source'),
            baseline=MagicMock(return_value={'installation': 'original'}), empty=MagicMock(), old_health=MagicMock())
        probes = {n: host(n, 'new-'+n, 0) for n in nodes}
        mutations = []
        def probe(lab, node, *, target=None, expected=None):
            if target is not None:
                progress = json.loads((lab.DIRECTORY/REC.RECORD).read_text())
                self.assertEqual(progress['hosts'][node]['phase'], 'write-intent')
                self.assertEqual(expected, probes[node])
                self.assertEqual(target, 1024)
                mutations.append(node)
                probes[node] = dict(probes[node], total=1024, free=1024)
            return copy.deepcopy(probes[node])
        def refresh(lab, node, pages):
            progress = json.loads((lab.DIRECTORY/REC.RECORD).read_text())
            self.assertEqual(progress['hosts'][node]['phase'], 'kubelet-refresh-intent')
            self.assertEqual(pages, 1024)
        resources = SimpleNamespace(record_path=RES.record_path, read_record=RES.read_record,
            validate_host=RES.validate_host, PAGES=RES.PAGES, probe=MagicMock(side_effect=probe),
            refresh_kubelet=MagicMock(side_effect=refresh), verify=MagicMock())
        return lab, resources, adopt, probes, mutations

    def run_recovery(self, lab, resources, adopt):
        with patch.object(REC, 'load', return_value=SimpleNamespace(active=lambda: False)), \
                patch.object(REC, 'disk_identity', return_value={'disks': 'stable'}), redirect_stdout(io.StringIO()):
            REC.recover(lab, resources, adopt, SimpleNamespace(qualification_input_current=lab.inputs_current))

    def test_exact_archive_actual_allocation_refresh_and_old_health(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, adopt, probes, mutations = self.fixture(directory)
            raw = RES.record_path(lab).read_bytes()
            self.run_recovery(lab, resources, adopt)
            new = RES.read_record(lab)
            archive = lab.DIRECTORY/new['reboot_recovery']['archive']
            self.assertEqual(archive.read_bytes(), raw)
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertEqual(new['reboot_recovery']['original_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(mutations, list(REC.NODES))
            self.assertEqual(resources.refresh_kubelet.call_count, 3)
            resources.verify.assert_called_once_with(lab)
            lab.installation_health.assert_called_once_with()
            adopt.previous_source.assert_not_called()
            adopt.old_health.assert_not_called()
            for n in REC.NODES:
                self.assertEqual(new['hosts'][n]['original']['boot_id'], 'new-'+n)
                self.assertEqual(new['hosts'][n]['original']['total'], 0)
                self.assertEqual(new['hosts'][n]['current']['total'], 1024)
            self.assertEqual(json.loads((lab.DIRECTORY/REC.RECORD).read_text())['phase'], 'complete')

    def test_one_reboot_preserves_other_hosts_and_never_refreshes_them(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, adopt, probes, mutations = self.fixture(directory)
            before = RES.read_record(lab)
            for node in REC.NODES[1:]:
                probes[node].update(boot_id='old-'+node, total=1024, free=0)
            self.run_recovery(lab, resources, adopt)
            after = RES.read_record(lab)
            self.assertEqual(mutations, [REC.NODES[0]])
            resources.refresh_kubelet.assert_called_once_with(lab, REC.NODES[0], 1024)
            for node in REC.NODES[1:]:
                self.assertEqual(after['hosts'][node], before['hosts'][node])
            REC.completed_transition(lab, lab.DIRECTORY/REC.RECORD, after)

    def test_same_boot_with_lost_pages_refuses_before_every_write(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, adopt, probes, mutations = self.fixture(directory)
            probes[REC.NODES[-1]]['boot_id'] = 'old-'+REC.NODES[-1]
            with self.assertRaisesRegex(ValueError, 'unrebooted'):
                self.run_recovery(lab, resources, adopt)
            self.assertFalse(mutations)

    def test_full_material_selects_current_accepted_installation_without_historical_fallback(self):
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as directory:
                lab, resources, adopt, probes, mutations = self.fixture(directory)
                current = {'status': 'pass', 'rc': 0, 'failure_markers': 0,
                           'remote_input_verification': {'mismatches': 0},
                           'independent_readback': {'original': True}}
                if not valid: current['rc'] = 1
                path = Path(directory)/'current-full-install.json'
                path.write_text(json.dumps(current))
                material = SimpleNamespace(active=lambda: True, path=MagicMock(return_value=path))
                with patch.object(REC, 'load', return_value=material), \
                        patch.object(REC, 'disk_identity', return_value={'disks': 'stable'}), redirect_stdout(io.StringIO()):
                    if valid:
                        REC.recover(lab, resources, adopt, SimpleNamespace(qualification_input_current=lab.inputs_current))
                        adopt.baseline.assert_called_with(lab, current, 'v2')
                    else:
                        with self.assertRaisesRegex(ValueError, 'accepted current full'):
                            REC.recover(lab, resources, adopt, SimpleNamespace(qualification_input_current=lab.inputs_current))
                        self.assertFalse(mutations)
                adopt.qualification.assert_not_called()

    def test_every_host_admitted_before_any_write(self):
        for field, value in (('boot_id', 'old-'+REC.NODES[-1]), ('total', 1), ('free', 1),
                             ('reserved', 1), ('available_kib', 1024), ('machine_id', 'foreign'),
                             ('kubelet_active', 'inactive')):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                lab, resources, adopt, probes, mutations = self.fixture(directory)
                probes[REC.NODES[-1]][field] = value
                raw = RES.record_path(lab).read_bytes()
                with self.assertRaises(ValueError): self.run_recovery(lab, resources, adopt)
                self.assertEqual(mutations, [])
                self.assertEqual(RES.record_path(lab).read_bytes(), raw)
                self.assertFalse((lab.DIRECTORY/REC.RECORD).exists())

    def test_later_reboot_archives_completed_transition_and_preserves_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, adopt, probes, mutations = self.fixture(directory)
            self.run_recovery(lab, resources, adopt)
            progress = lab.DIRECTORY/REC.RECORD
            completed = progress.read_bytes()
            # An intervening owned boot fixture updates the validated ledger.
            ledger = RES.read_record(lab)
            for node in REC.NODES:
                for key in ('original', 'current'):
                    ledger['hosts'][node][key]['boot_id'] = 'fixture-'+node
                probes[node].update(boot_id='later-'+node, total=0, free=0)
            lab.write_private(RES.record_path(lab), ledger)
            before = RES.record_path(lab).read_bytes()
            self.run_recovery(lab, resources, adopt)
            transition = json.loads(progress.read_text())
            prior = transition['previous_transition']
            self.assertEqual((lab.DIRECTORY/prior['archive']).read_bytes(), completed)
            self.assertEqual(prior['sha256'], hashlib.sha256(completed).hexdigest())
            self.assertEqual((lab.DIRECTORY/transition['archive']).read_bytes(), before)
            self.assertEqual(transition['phase'], 'complete')
            self.assertEqual(mutations, list(REC.NODES)*2)

    def test_completed_transition_mismatch_refuses_before_probe_or_write(self):
        for defect in ('phase', 'run', 'cluster', 'resource', 'receipt', 'archive', 'machine', 'symlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, resources, adopt, probes, mutations = self.fixture(directory)
                self.run_recovery(lab, resources, adopt)
                progress = lab.DIRECTORY/REC.RECORD
                old = json.loads(progress.read_text())
                ledger = RES.read_record(lab)
                if defect == 'phase': old['phase'] = 'resource-journal-update-intent'
                elif defect in ('run', 'cluster', 'resource'): old[defect+'_id' if defect != 'cluster' else 'cluster_uid'] = 'foreign'
                elif defect == 'receipt': ledger['reboot_recovery']['original_sha256'] = 'foreign'
                elif defect == 'archive': (lab.DIRECTORY/old['archive']).write_bytes(b'changed')
                elif defect == 'machine': old['hosts'][REC.NODES[0]]['current']['machine_id'] = 'foreign'
                progress.write_text(json.dumps(old))
                if defect == 'symlink':
                    saved = lab.DIRECTORY/'saved'; progress.rename(saved); progress.symlink_to(saved)
                lab.write_private(RES.record_path(lab), ledger)
                before = RES.record_path(lab).read_bytes()
                calls = resources.probe.call_count
                with self.assertRaises(ValueError): self.run_recovery(lab, resources, adopt)
                self.assertEqual(resources.probe.call_count, calls)
                self.assertEqual(RES.record_path(lab).read_bytes(), before)
                self.assertEqual(mutations, list(REC.NODES))

    def test_completed_receipt_is_retained_when_new_boot_admission_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, adopt, probes, mutations = self.fixture(directory)
            self.run_recovery(lab, resources, adopt)
            progress = lab.DIRECTORY/REC.RECORD
            before = progress.read_bytes()
            with self.assertRaisesRegex(ValueError, 'changed boot'):
                self.run_recovery(lab, resources, adopt)
            self.assertEqual(progress.read_bytes(), before)
            self.assertEqual(mutations, list(REC.NODES))

    def test_source_baseline_consumers_or_wwn_refusal_never_mutates(self):
        for scope in ('inputs_current', 'baseline', 'empty', 'disk_identity', 'instances'):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as directory:
                lab, resources, adopt, probes, mutations = self.fixture(directory)
                if scope == 'instances':
                    lab.get.return_value['items'][0]['status']['instanceReplicas'] = {'foreign': {}}
                elif scope == 'inputs_current':
                    lab.inputs_current.return_value = False
                elif scope != 'disk_identity':
                    getattr(adopt, scope).side_effect = ValueError('refuse')
                with patch.object(REC, 'disk_identity', side_effect=ValueError('changed WWN') if scope == 'disk_identity' else None,
                                  return_value={'disks': 'stable'}):
                    with self.assertRaises(ValueError): REC.recover(lab, resources, adopt, SimpleNamespace(qualification_input_current=lab.inputs_current))
                self.assertEqual(mutations, [])

    def test_lost_response_retains_intent_original_journal_and_rejects_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, adopt, probes, mutations = self.fixture(directory)
            raw = RES.record_path(lab).read_bytes()
            original = resources.probe.side_effect
            def fail(lab, node, **kwargs):
                if kwargs.get('target') is not None: raise OSError('SSH reply lost')
                return original(lab, node, **kwargs)
            resources.probe.side_effect = fail
            with self.assertRaises(OSError): self.run_recovery(lab, resources, adopt)
            self.assertEqual(RES.record_path(lab).read_bytes(), raw)
            progress = json.loads((lab.DIRECTORY/REC.RECORD).read_text())
            self.assertEqual(progress['hosts'][REC.NODES[0]]['phase'], 'write-intent')
            calls = resources.probe.call_count
            with self.assertRaisesRegex(ValueError, 'intent requires diagnosis'):
                self.run_recovery(lab, resources, adopt)
            self.assertEqual(resources.probe.call_count, calls)
            resources.refresh_kubelet.assert_not_called()

    def test_partial_allocation_or_refresh_failure_retains_first_confirmed_state(self):
        for fault in ('partial', 'refresh'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, resources, adopt, probes, mutations = self.fixture(directory)
                raw = RES.record_path(lab).read_bytes()
                if fault == 'refresh':
                    resources.refresh_kubelet.side_effect = TimeoutError('node not ready')
                else:
                    original = resources.probe.side_effect
                    def partial(lab, node, **kwargs):
                        result = original(lab, node, **kwargs)
                        if kwargs.get('target') is not None: result.update(total=512, free=512)
                        return result
                    resources.probe.side_effect = partial
                with self.assertRaises((ValueError, TimeoutError)): self.run_recovery(lab, resources, adopt)
                self.assertEqual(RES.record_path(lab).read_bytes(), raw)
                progress = json.loads((lab.DIRECTORY/REC.RECORD).read_text())
                self.assertEqual(progress['hosts'][REC.NODES[0]]['phase'],
                                 'write-confirmed' if fault == 'partial' else 'kubelet-refresh-intent')
                adopt.old_health.assert_not_called()

    def test_archive_is_exclusive_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'foreign'; target.write_bytes(b'foreign')
            link = Path(directory)/'archive'; link.symlink_to(target)
            with self.assertRaises(FileExistsError): REC.archive_bytes(link, b'new')
            self.assertEqual(target.read_bytes(), b'foreign')

    def target_evidence(self):
        return {'status':'pass','rc':0,'failure_markers':0,
                'remote_input_verification':{'mismatches':0},
                'baseline_identity':{'target':'original'},
                'independent_readback':{'baseline_identity':{'target':'original'}}}

    def test_target_uses_only_independent_single_node_resource_verifier_and_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, adopt, probes, mutations = self.fixture(directory, REC.TARGET_NODES)
            verify = MagicMock()
            output=io.StringIO()
            with patch.object(REC, 'disk_identity', return_value={'target':'stable'}), redirect_stdout(output):
                REC.recover(lab,resources,adopt,SimpleNamespace(qualification_input_current=lab.inputs_current),profile='target',evidence=self.target_evidence(),verify=verify)
            self.assertEqual(mutations,list(REC.TARGET_NODES))
            verify.assert_called_once_with(lab)
            resources.verify.assert_not_called()
            adopt.qualification.assert_not_called()
            self.assertIn('LONGHORN_V2_DR_TARGET_RESOURCES_REBOOT_RECOVERY_SCOPED_CLEAN_PASS',output.getvalue())
            self.assertNotIn('LONGHORN_V2_RESOURCES_REBOOT_RECOVERY_SCOPED_CLEAN_PASS',output.getvalue())

    def test_cross_pool_or_unqualified_target_is_rejected_before_write(self):
        for failure in ('source-as-target','target-as-source','evidence','verifier'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                nodes=REC.NODES if failure=='source-as-target' else REC.TARGET_NODES
                lab, resources, adopt, probes, mutations = self.fixture(directory,nodes)
                evidence=self.target_evidence()
                if failure=='evidence':evidence['remote_input_verification']['mismatches']=1
                options={} if failure=='target-as-source' else dict(profile='target',evidence=evidence,
                    verify=None if failure=='verifier' else MagicMock())
                with patch.object(REC,'disk_identity'):
                    with self.assertRaises(ValueError):REC.recover(lab,resources,adopt,SimpleNamespace(qualification_input_current=lab.inputs_current),**options)
                self.assertEqual(mutations,[])

    def test_real_shared_runner_and_static_wiring_include_both_explicit_modes(self):
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        entry=(ROOT/'tests/run_enterprise_regression.sh').read_text()
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        static=(ROOT/'tests/helpers/longhorn-static.sh').read_text()
        for mode in REC.MODES:
            self.assertIn('--longhorn-'+mode,runner)
            self.assertIn(mode,live)
            self.assertEqual(runner.count(mode+'|'),2)
        self.assertIn('tests/helpers/longhorn-runner.sh',entry)
        self.assertIn('longhorn-v2-resources-reboot.sh',live)
        self.assertIn('tests/helpers/longhorn-v2-resources-reboot.py',static)
        self.assertIn('tests.unit.test_longhorn_v2_resources_reboot',static)

    def test_shell_retains_failure_resources_and_durable_exit_without_full_ga_claim(self):
        shell = (ROOT/'tests/helpers/longhorn-v2-resources-reboot.sh').read_text()
        self.assertIn('set -Eeuo pipefail', shell)
        self.assertIn('"$rc" > "$work/exit"', shell)
        self.assertIn('resources=retained', shell)
        self.assertNotIn('"$RESOURCES" restore', shell)
        self.assertIn('echo "${marker}_PASS"', shell)
        for mode in REC.MODES:self.assertIn(mode,shell)
        self.assertNotIn('LONGHORN_V2_PLATFORM_PASS', shell)


if __name__ == '__main__': unittest.main()
