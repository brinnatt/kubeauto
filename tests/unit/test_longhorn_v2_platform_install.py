import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


HELPER = Path(__file__).resolve().parents[1]/'helpers/longhorn-v2-platform-install.py'
spec = importlib.util.spec_from_file_location('canonical_v2', HELPER)
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


class EvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.original = gate.BASE; gate.BASE = self.root
        self.addCleanup(setattr, gate, 'BASE', self.original)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def stage(self, kind='platform'):
        spec = gate.SPECS[kind]
        directory = self.root/'logs/longhorn-cases'/(spec['module']+'-20261021-190000')
        directory.mkdir(parents=True)
        before = {'run_id': 'run', 'config_sha256': 'config',
            'baseline_identity': {'cluster_uid': 'core', 'namespace_uid': 'namespace', 'node_uids': {'node': 'uid'}},
            'native_block_receipt_sha256': 'receipt', 'hardware': {
                node: {'machine_id': 'machine-'+node, 'disk': {'path': '/dev/disk/by-id/verified-'+node}}
                for node in gate.HOSTS}}
        prior = {'kind': kind, 'original_baseline_identity': copy.deepcopy(before['baseline_identity']),
            'original_native_block_receipt_sha256': 'receipt',
            'machine_ids': {node: 'machine-'+node for node in gate.HOSTS},
            'original_native_disks': {node: {'disks': {'disk': copy.deepcopy(before['hardware'][node]['disk'])}}
                                      for node in gate.HOSTS}}
        row = {'baseline_before': before, 'baseline_after': copy.deepcopy(before)}
        if kind == 'platform':
            row['profiles'] = {n: {'state': {'runtime': 'actual'}, 'ledger': {'readback': 'sha'}, 'host_budget': {'memory': 8}}
                for n in ('anonymous-iobuf', 'exclusive', 'original')}
            row['profiles']['combined'] = {'safe_refusal': True, 'ordinary_limits': ['128Mi']}
            extra = ['LONGHORN_V2_RUNTIME_COMBINATION_SAFE_REFUSAL_PASS']+[
                'LONGHORN_V2_RUNTIME_PROFILE_PASS profile='+name
                for name in ('anonymous-iobuf', 'exclusive', 'original')]
        else:
            row.update(idempotent=True, business=[{'engine': engine, 'volume_uid': 'volume-'+engine,
                        'sha256': 'a'*64, 'cross_node': True} for engine in ('v1', 'v2')],
                       managers={str(i): {'engine': 'v1' if i < 3 else 'v2'} for i in range(6)},
                       disk_uuids={engine: {node: engine+node for node in gate.HOSTS} for engine in ('v1', 'v2')}); extra = []
        self.write(directory/'gate.log', '\n'.join([spec['result']+json.dumps(row)]+list(spec['markers'])+extra+['LONGHORN_GATE_EXIT rc=0'])+'\n')
        for name in ('durable-exit', 'durable-finalized'):
            self.write(directory/name, '0\n')
        self.write(directory/'durable-status.txt', 'state=exited rc=0 failure_markers=0\n')
        return directory, prior, row

    def rewrite(self, directory, row):
        path = directory/'gate.log'; lines = path.read_text().splitlines()
        prefix = next(s for s in ('LONGHORN_V2_RUNTIME_RESOURCES_RESULT ', 'LONGHORN_V2_COEXIST_RESULT ') if lines[0].startswith(s))
        lines[0] = prefix+json.dumps(row); path.write_text('\n'.join(lines)+'\n')

    def finish(self, directory, prior):
        with patch.object(gate, 'preflight', return_value=prior), redirect_stdout(io.StringIO()):
            return gate.finish(prior['kind'], prior, directory)

    def test_runtime_complete_receipt(self):
        directory, prior, _ = self.stage()
        result = self.finish(directory, prior)
        self.assertEqual(result['durable'], {'exit': 0, 'finalized': 0})
        self.assertTrue((directory/'acceptance.json').is_file())

    def test_coexist_complete_receipt(self):
        directory, prior, _ = self.stage('install')
        self.assertEqual(self.finish(directory, prior)['kind'], 'install')

    def test_duplicate_success_marker_rejected(self):
        directory, prior, _ = self.stage()
        with (directory/'gate.log').open('a') as stream:
            stream.write(gate.SPECS['platform']['markers'][-1]+'\n')
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            self.finish(directory, prior)

    def test_failure_marker_rejected_even_with_success(self):
        directory, prior, _ = self.stage()
        with (directory/'gate.log').open('a') as stream: stream.write('LONGHORN_FIRST_FAILURE rc=1\n')
        with self.assertRaisesRegex(ValueError, 'failure marker'): self.finish(directory, prior)

    def test_finalizer_missing_or_failed_is_not_accepted(self):
        for value in (None, '1\n'):
            with self.subTest(value=value):
                directory, prior, _ = self.stage()
                path = directory/'durable-finalized'
                if value is None: path.unlink()
                else: path.write_text(value)
                with self.assertRaises(ValueError): self.finish(directory, prior)
                import shutil
                shutil.rmtree(directory)

    def test_changed_input_stops_composition(self):
        directory, prior, _ = self.stage()
        with patch.object(gate, 'preflight', return_value={}):
            with self.assertRaisesRegex(ValueError, 'changed during'): gate.finish('platform', prior, directory)

    def test_foreign_baseline_is_not_relabelled_pass(self):
        directory, prior, row = self.stage()
        row['baseline_before']['baseline_identity']['cluster_uid'] = 'other'
        row['baseline_after'] = copy.deepcopy(row['baseline_before']); self.rewrite(directory, row)
        with self.assertRaisesRegex(ValueError, 'different installation'): self.finish(directory, prior)

    def test_changed_machine_or_disk_is_rejected(self):
        directory, prior, row = self.stage()
        row['baseline_before']['hardware']['192.168.47.146']['machine_id'] = 'replacement'
        row['baseline_after'] = copy.deepcopy(row['baseline_before']); self.rewrite(directory, row)
        with self.assertRaisesRegex(ValueError, 'different hardware'): self.finish(directory, prior)

    def test_scope_cleanup_must_restore_input_identity(self):
        directory, prior, row = self.stage()
        row['baseline_after']['config_sha256'] = 'modified'; self.rewrite(directory, row)
        with self.assertRaisesRegex(ValueError, 'restore'): self.finish(directory, prior)

    def test_missing_runtime_or_combination_evidence_rejected(self):
        for key in ('ledger', 'combined'):
            with self.subTest(key=key):
                directory, prior, row = self.stage()
                if key == 'ledger': del row['profiles']['exclusive']['ledger']
                else: del row['profiles']['combined']
                self.rewrite(directory, row)
                with self.assertRaises(ValueError): self.finish(directory, prior)
                import shutil
                shutil.rmtree(directory)

    def test_incomplete_coexistence_rejected(self):
        directory, prior, row = self.stage('install')
        row['idempotent'] = False; self.rewrite(directory, row)
        with self.assertRaisesRegex(ValueError, 'incomplete'): self.finish(directory, prior)

    def test_raw_path_traversal_and_symlink_rejected(self):
        with self.assertRaises(ValueError): gate.relative('../outside')
        target = self.write(self.root/'raw', 'hello'); link = self.root/'alias'; link.symlink_to(target)
        with self.assertRaises(ValueError): gate.digest(link)

    def test_historical_sha_mismatch_rejected_before_consuming_marker(self):
        name = 'tests/evidence/longhorn-v2-host-qualification-20261020.json'
        raw = self.write(self.root/'logs/gate.log', 'LONGHORN_V2_HOST_QUALIFICATION_PASS\n')
        evidence = {'status': 'pass', 'rc': 0, 'failure_markers': 0,
            'remote_input_verification': {'mismatches': 0},
            'raw_logs': {'logs/gate.log': hashlib.sha256(b'other').hexdigest()}}
        self.write(self.root/name, json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, 'raw digest changed'): gate.historical('host')

    def test_parallel_stage_reuses_explicit_directory_without_launching(self):
        directory, prior, row = self.stage()
        self.write(directory/'baseline-before.json', json.dumps(row['baseline_before']))
        self.write(directory/'baseline-after.json', json.dumps(row['baseline_after']))
        with redirect_stdout(io.StringIO()): result = gate.verify_stage('v2-runtime-resources', directory)
        self.assertEqual(result['durable']['finalized'], 0)

    def test_parallel_stage_identity_change_stops_next_stage(self):
        directory, prior, row = self.stage()
        self.write(directory/'baseline-before.json', json.dumps(row['baseline_before']))
        row['baseline_after']['config_sha256'] = 'other'
        self.write(directory/'baseline-after.json', json.dumps(row['baseline_after']))
        with self.assertRaisesRegex(ValueError, 'independently read'):
            gate.verify_stage('v2-runtime-resources', directory)

    def test_incomplete_parallel_stage_cannot_publish_clean_summary(self):
        directory, _, _ = self.stage()
        (directory/'durable-finalized').write_text('1\n')
        (directory/'durable-status.txt').unlink()
        with self.assertRaisesRegex(ValueError, 'durable fences'):
            gate.verify_stage('v2-runtime-resources', directory)
        self.assertFalse((directory/'durable-status.txt').exists())

    def test_fresh_full_result_uses_same_assertions_without_old_namespace(self):
        directory, _, row = self.stage()
        row['baseline_before']['baseline_identity']['namespace_uid'] = 'fresh-namespace'
        row['baseline_after'] = copy.deepcopy(row['baseline_before'])
        result = gate.validate_result('platform', row, (directory/'gate.log').read_text().splitlines())
        self.assertEqual(result['baseline_identity']['namespace_uid'], 'fresh-namespace')

    def test_fixed_batch_keeps_three_owner_stages_serial_and_source_only(self):
        text = (HELPER.parent/'longhorn-parallel.sh').read_text()
        block = text.split('  v2-platform-install|v2-install-tail)\n', 1)[1].split('    ;;', 1)[0]
        self.assertIn('modes=(v2-runtime-resources v2-coexist-qualification v2-maintenance-drain-qualification)', block)
        self.assertIn('modes=(v2-coexist-qualification v2-maintenance-drain-qualification)', block)
        self.assertIn('if [[ "$1" == v2-install-tail ]]', block)
        self.assertIn('for mode in "${modes[@]}";', block)
        self.assertIn('run-durable-gate.sh', block)
        self.assertIn('baseline-before.json', block)
        self.assertIn('baseline-after.json', block)
        self.assertLess(block.index('baseline-before.json'), block.index('run-durable-gate.sh'))
        self.assertGreater(block.index('baseline-after.json'), block.index('owning.finalized'))
        self.assertNotIn('192.168.47.149', block)
        self.assertNotIn('longhorn_launch', block)
        self.assertNotIn(' || true', block)

    def test_canonical_public_routes_use_exact_explicit_launcher_result(self):
        text = (HELPER.parent/'longhorn-runner.sh').read_text()
        self.assertIn('--longhorn-v2-platform) longhorn_v2_platform_install platform', text)
        self.assertIn('--longhorn-v2-install) longhorn_v2_platform_install install', text)
        self.assertIn('LONGHORN_LAST_DIRECTORY="$directory"', text)
        self.assertIn('--directory "$LONGHORN_LAST_DIRECTORY"', text)
        self.assertNotIn('ls -t', text)


class ResourceReaderReuseContracts(unittest.TestCase):
    def fixture(self, root):
        review_name='tests/evidence/longhorn-v2-resources-reader-reuse-20261022.json'
        source=Path(__file__).resolve().parents[2]/'resources-reader-review.json'
        # In the product tree the exact reviewed file is committed evidence.
        if not source.exists():source=HELPER.parents[2]/review_name
        raw=source.read_bytes();review=json.loads(raw)
        for key in ('mechanism_review','resolver','completed_installation_review'):
            ref=review[key];actual=HELPER.parents[2]/ref['path']
            target=root/ref['path'];target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(actual.read_bytes())
        target=root/review_name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        return review

    def test_exact_historical_helper_delta_and_all_dependency_guards(self):
        for defect in ('none','unknown-old','unknown-new','review','mechanism_review','resolver','completed_installation_review','full','empty-full'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);review=self.fixture(root);helper=review['resource_helper'];old,new=helper['old'],helper['current']
                if defect=='unknown-old':old='f'*64
                elif defect=='unknown-new':new='f'*64
                elif defect=='review':(root/gate.RESOURCE_REUSE[0]).write_text('{}')
                elif defect in ('mechanism_review','resolver','completed_installation_review'):
                    path=root/review[defect]['path'];path.write_bytes(path.read_bytes()+b'\n')
                env={'LONGHORN_FULL_EVIDENCE_DIR':'' if defect=='empty-full' else '/foreign'} if 'full' in defect else {}
                with patch.object(gate,'BASE',root),patch.dict('os.environ',env):
                    if defect=='none':self.assertTrue(gate.reviewed_resource_input(helper['path'],old,new))
                    else:
                        with self.assertRaises(ValueError):gate.reviewed_resource_input(helper['path'],old,new)
                    self.assertFalse(gate.reviewed_resource_input('foreign.py',old,new))

if __name__ == '__main__': unittest.main()
