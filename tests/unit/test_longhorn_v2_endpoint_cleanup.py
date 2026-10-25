"""Retired V2 source endpoints never bypass native deletion or host leases."""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('endpoint_cleanup_lab', ROOT/'tests/helpers/longhorn-lab.py')
LAB = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(LAB)
SPEC = importlib.util.spec_from_file_location('endpoint_cleanup_prepare', ROOT/'tests/helpers/longhorn-v2-upgrade-prepare.py')
PREP = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(PREP)
NAME = 'pvc-98bd52db-e763-4bfe-99c2-152e71257206'
UID = 'e891224b-315e-4487-913c-d92a7847b625'


class EndpointContracts(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        for folder in ('etc', 'sys/class/block', 'sys/dev/block', 'proc/1'):
            (root/folder).mkdir(parents=True)
        (root/'etc/machine-id').write_text('original')
        (root/'proc/1/mountinfo').write_text('1 0 8:1 / / rw - ext4 /dev/sda1 rw\n')
        for device, name, uuid in [('dm-0', 'system-lvm', 'LVM-original'), ('dm-1', NAME, '')]:
            p = root/'sys/class/block'/device
            (p/'dm').mkdir(parents=True); (p/'holders').mkdir()
            (p/'dm/name').write_text(name); (p/'dm/uuid').write_text(uuid)
            (p/'dev').write_text('252:'+device[-1])
        request = {'volume': NAME, 'machine_id': 'original', 'size_bytes': 2*1024**3}
        def run(argv, **kwargs):
            if argv[:2] == ['dmsetup', 'info']: output = NAME+'||252|1|0|1'
            elif argv[:2] == ['dmsetup', 'table']: output = '0 4194304 linear 259:1 0'
            elif argv == ['dmsetup', 'remove', NAME]:
                import shutil
                shutil.rmtree(root/'sys/class/block/dm-1'); output = ''
            else: raise AssertionError(argv)
            return SimpleNamespace(stdout=output)
        return root, request, run

    def test_only_exact_idle_orphan_is_removed_without_force_and_system_map_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root, request, run = self.fixture(d)
            with patch('subprocess.run', side_effect=run) as commands:
                result = LAB.retired_v2_endpoint_host(request, root=root)
            self.assertTrue(result['removed']); self.assertTrue(result['absent'])
            self.assertEqual(['dmsetup', 'remove', NAME], commands.call_args_list[-1].args[0])
            self.assertEqual('LVM-original', (root/'sys/class/block/dm-0/dm/uuid').read_text())
            with patch('subprocess.run') as commands:
                self.assertFalse(LAB.retired_v2_endpoint_host(request, root=root)['removed'])
                commands.assert_not_called()

    def test_foreign_machine_uuid_busy_mounted_holder_live_lower_or_changed_table_cannot_remove(self):
        for defect in ('machine', 'name', 'uuid', 'busy', 'mount', 'holder', 'lower', 'size', 'table'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                root, request, run = self.fixture(d)
                if defect == 'machine': request['machine_id'] = 'foreign'
                if defect == 'name': request['volume'] = 'system-lvm'
                if defect == 'uuid': (root/'sys/class/block/dm-1/dm/uuid').write_text('foreign')
                if defect == 'mount': (root/'proc/1/mountinfo').write_text('1 0 252:1 / / rw - ext4 /dev/dm-1 rw\n')
                if defect == 'holder': (root/'sys/class/block/dm-1/holders/foreign').touch()
                if defect == 'lower': (root/'sys/dev/block/259:1').mkdir()
                if defect == 'size': request['size_bytes'] *= 2
                def changed(argv, **kwargs):
                    row = run(argv, **kwargs)
                    if defect == 'busy' and argv[1] == 'info': row.stdout = NAME+'||252|1|1|1'
                    if defect == 'table' and argv[1] == 'table': row.stdout = '0 4194304 error'
                    return row
                with patch('subprocess.run', side_effect=changed) as commands, self.assertRaises(ValueError):
                    LAB.retired_v2_endpoint_host(request, root=root)
                self.assertFalse(any(c.args[0][1] == 'remove' for c in commands.call_args_list))

    def test_endpoint_recovery_runs_only_after_native_cleanup_and_before_business_journal_removal(self):
        text = (ROOT/'tests/helpers/longhorn-lab.py').read_text()
        body = text[text.index('def cleanup_business('):text.index('\ndef backup_store(')]
        self.assertLess(body.index("wait('business-namespace-clean'"), body.index('cleanup_retired_v2_endpoint('))
        self.assertLess(body.index('cleanup_retired_v2_endpoint('), body.index('record.unlink()'))
        self.assertIn("if journal.get('v2_all_replicas_fault'):", body)

    def recovery(self):
        observed = {'source_volume_uid': UID, 'source_fenced': {'volume_uid': UID, 'writer_absent': True, 'volume_state': 'detached'},
                    'refusal': {'replicas': {NAME+'-r-a': {}, NAME+'-r-b': {}, NAME+'-r-c': {}}}}
        evidence = {'signed_scope': 'v2-failure/all-replicas', 'status': 'pass', 'durable': {'exit': 0, 'finalized': 0},
                    'raw_evidence': {'original/all-replicas.log': 'sha'}, 'observation': observed}
        runtime = {'baseline': {'original': True}, 'failure_scopes': {'all-replicas': {'path': 'evidence'}}}
        row = {'baseline_before': runtime['baseline'], 'previous_evidence': {'runtime_continuation': {'path': 'runtime'}}}
        log = 'LONGHORN_V2_ALL_REPLICAS_RECOVERY '+json.dumps(observed)+'\nLONGHORN_DELETE_POLICY_RECLAIM_VERIFIED volume='+NAME+'\n'
        source = SimpleNamespace(get=Mock(side_effect=lambda kind, *a, **kw: None if kind == 'crd' else {'items': []}),
                                 cleanup_retired_v2_endpoint=Mock())
        scenario = PREP.Preparation.__new__(PREP.Preparation); scenario.source = source
        def ref(item):
            return SimpleNamespace(read_text=lambda: {'runtime': json.dumps(runtime), 'evidence': json.dumps(evidence),
                                                      'original/all-replicas.log': log}[item['path']])
        return scenario, source, row, runtime, evidence, ref

    def test_retirement_reuses_original_fenced_deleted_source_without_rewriting_business_evidence(self):
        scenario, source, row, _, _, ref = self.recovery()
        with patch.object(PREP.baseline, 'referenced', side_effect=ref): scenario.recover_retired_endpoint(row)
        source.cleanup_retired_v2_endpoint.assert_called_once_with(NAME, UID, 2*1024**3)

    def test_recovery_rejects_changed_runtime_incomplete_original_scope_or_current_consumers(self):
        for defect in ('runtime', 'durable', 'scope', 'writer', 'native', 'pods', 'pv'):
            with self.subTest(defect=defect):
                scenario, source, row, runtime, evidence, ref = self.recovery()
                if defect == 'runtime': runtime['baseline'] = {'foreign': True}
                if defect == 'durable': evidence['durable']['exit'] = 1
                if defect == 'scope': evidence['signed_scope'] = 'foreign'
                if defect == 'writer': evidence['observation']['source_fenced']['writer_absent'] = False
                if defect in ('native', 'pods', 'pv'):
                    source.get.side_effect = lambda kind, *a, **kw: ({'metadata': {}} if defect == 'native' else None) if kind == 'crd' else {'items': [{'spec': {'csi': {'driver': 'driver.longhorn.io'}}}] if kind == {'pods': 'pods', 'pv': 'pv'}.get(defect) else []}
                with patch.object(PREP.baseline, 'referenced', side_effect=ref), self.assertRaises(ValueError):
                    scenario.recover_retired_endpoint(row)
                source.cleanup_retired_v2_endpoint.assert_not_called()


if __name__ == '__main__': unittest.main()
