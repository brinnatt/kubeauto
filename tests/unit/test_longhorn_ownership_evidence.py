"""Deterministic import contracts; no lab or historical evidence is required."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('LONGHORN_OWNERSHIP_TEST_SOURCE_ROOT', str(ROOT)))
spec = importlib.util.spec_from_file_location('ownership_reader', ROOT / 'tests/helpers/longhorn-ownership-evidence.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class OwnershipEvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archives = {key: self.root / key for key in reader.VERIFIERS}
        self.data = {'case': 'LONGHORN-03', 'version': '1.12.1',
                     'instance_manager_version': 'v1.12.1-kubeauto.2',
                     'input_sets': {}, 'stages': {}, 'raw_evidence': {}}
        dependencies = {'common/fixture{}.py'.format(i): sha(b'product') for i in range(106)}
        self.data['unchanged_product_execution_dependencies'] = dependencies
        verifiers = {}
        for key, (name, _) in reader.VERIFIERS.items():
            verifiers[key] = (name, sha((SOURCE / name).read_bytes()))
        self.verifier_patch = patch.dict(reader.VERIFIERS, verifiers)
        self.verifier_patch.start()
        self.addCleanup(self.verifier_patch.stop)
        for name in dependencies:
            self.write(name, b'product')
        for key, archive in self.archives.items():
            inputs = dict(dependencies)
            for _, (name, digest) in reader.VERIFIERS.items():
                inputs[name] = digest
            count = int(key[-3:])
            for i in range(count - len(inputs)):
                inputs['tests/fixture{}.txt'.format(i)] = sha(b'fixture')
            for name in inputs:
                value = (SOURCE / name).read_bytes() if name.startswith('tests/helpers/') else b'product' if name in dependencies else b'fixture'
                self.write(str(archive.relative_to(self.root) / 'frozen-source' / name), value)
            manifest = json.dumps(inputs, sort_keys=True).encode()
            self.write(str(archive.relative_to(self.root) / 'frozen-inputs.json'), manifest)
            self.data['input_sets'][key] = {'frozen_inputs': inputs, 'manifest_sha256': sha(manifest),
                                             'verified_before_and_after': count}
        spec = importlib.util.spec_from_file_location('fixture_parallel', SOURCE / 'tests/helpers/longhorn-parallel.py')
        parallel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parallel)
        owner = {'run_id': 'fixture-run', 'node_uids': {str(i): str(i) for i in range(6)},
                 'healthy': True, 'clean_start_verified': True,
                 'disks': {str(i): {'uuid': 'disk' + str(i), 'size': 107374182400} for i in range(3)}}
        continuity = {'owner': owner, 'prior_run_id_evidence': {'path': 'raw/prior.json'},
                      'shared_context_sha256_preserved': sha(b'context'),
                      'request_confirmed_bytes': 9437184, 'request_confirmed_sha256': sha(b'data')}
        self.data['baseline_continuity'] = continuity
        guard = ['LONGHORN_CORE_IDENTITY_PASS nodes=6', 'LONGHORN_LAB_INSTALLATION_HEALTHY']
        checksum = 'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256={} replica_nodes=fixture'.format(sha(b'data'))
        for stage in reader.STAGES:
            original = stage in reader.STAGES[:4]
            key = 'original777' if original else 'continuation782'
            marker = 'LONGHORN_' + stage.upper().replace('-', '_')
            lines = guard + ([marker + '_SCOPED_CLEAN_PASS', marker + '_PASS', marker + '_EXIT rc=0']
                             if original else list(parallel.MARKERS[stage]))
            if stage == 'install':
                lines += ['LONGHORN_LAB_DISK_READY node={} uuid={} size={}'.format(host, disk['uuid'], disk['size'])
                          for host, disk in owner['disks'].items()]
            if stage in ('install', reader.STAGES[-1]):
                lines += [checksum]
            prefix = 'raw/' + stage + '/' + (stage if original else 'v1')
            raw = {}
            for suffix, value in (('.exit', b'0\n'), ('.finalized', b'0\n'),
                                  ('.pid', b'123\n'), ('.log', ('\n'.join(lines) + '\n').encode())):
                name = prefix + suffix
                self.write(name, value)
                raw[name] = sha(value)
            self.data['raw_evidence'].update(raw)
            source, digest = reader.VERIFIERS[key]
            record = {'input_set': key, 'exit': 0, 'finalized': 0, 'failure_markers': 0,
                      'raw': raw, 'owning_verifier': {'path': source, 'sha256': digest}}
            if original:
                record['original_frozen_verifier'] = 'LONGHORN_OWNERSHIP_STAGE_VERIFIED stage=' + stage + ' durable_rc=0 finalized=0'
            else:
                record['owning_verifier']['branch'] = stage
                record['outer_exit_marker'] = 'LONGHORN_PARALLEL_V1_EXIT rc=0'
                record['verification'] = parallel.verify_branch(self.root / 'raw' / stage, stage)
            self.data['stages'][stage] = record
        self.raw_json('raw/prior.json', {'rendered_summaries': {'yml/longhorn/input.json': {
            'nonsecret_fields': {'longhorn_storage_classes': [{'name': 'longhorn-owner-fixture-run'}]}}}})
        self.raw_json('raw/context.json', {'exists': True, 'mode': 0o400, 'uid': 0, 'sha256': sha(b'context')})
        self.raw_json('raw/capture.json', {'baseline': {'owner': owner, 'shared_context': {'sha256': sha(b'context')},
            'ownership_journals': {str(i): {'exists': False} for i in range(3)}},
            'frozen_inputs': self.data['input_sets']['continuation782']['frozen_inputs'],
            'source_map_sha256': self.data['input_sets']['continuation782']['manifest_sha256'],
            'verified_before_and_after': 782})

    def write(self, name, raw):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def raw_json(self, name, value):
        raw = json.dumps(value, sort_keys=True).encode()
        self.write(name, raw)
        self.data['raw_evidence'][name] = sha(raw)

    def consume(self, digest=None):
        raw = json.dumps(self.data, sort_keys=True).encode()
        path = self.write('request.json', raw)
        return reader.consume(self.root, path, digest or sha(raw), self.archives)

    def test_complete_six_stages_are_consumed_without_promoting_failed_parent(self):
        result = self.consume()
        self.assertEqual(len(result['stages']), 6)
        self.assertEqual(result['product_dependencies'], 106)
        self.assertFalse(result['old_failed_parent_promoted'])

    def test_missing_stage_wrong_version_rc_source_and_identity_are_rejected(self):
        pristine = copy.deepcopy(self.data)
        for defect in ('missing', 'version', 'rc', 'source', 'identity', 'input-set', 'dependency'):
            with self.subTest(defect=defect):
                self.data = copy.deepcopy(pristine)
                stage = self.data['stages'][reader.STAGES[4]]
                if defect == 'missing': del self.data['stages'][reader.STAGES[0]]
                elif defect == 'version': self.data['version'] = '1.12.0'
                elif defect == 'rc': stage['exit'] = 1
                elif defect == 'source': stage['owning_verifier']['sha256'] = '0' * 64
                elif defect == 'identity': self.data['baseline_continuity']['owner']['run_id'] = 'foreign'
                elif defect == 'input-set': stage['input_set'] = 'original777'
                else: self.data['unchanged_product_execution_dependencies'].pop('common/fixture0.py')
                with self.assertRaises(ValueError): self.consume()

    def test_request_hash_and_current_product_change_are_rejected(self):
        with self.assertRaises(ValueError): self.consume('0' * 64)
        self.write('common/fixture0.py', b'changed')
        with self.assertRaises(ValueError): self.consume()

    def test_duplicate_marker_and_failed_old_controller_cannot_be_imported(self):
        pristine = copy.deepcopy(self.data)
        stage = reader.STAGES[4]
        name = 'raw/' + stage + '/v1.log'
        original = (self.root / name).read_bytes()
        for defect in ('duplicate', 'failed-controller', 'missing-finalized'):
            with self.subTest(defect=defect):
                self.data = copy.deepcopy(pristine)
                value = original + (b'LONGHORN_OWNERSHIP_CONTROLLER_REFUSAL_QUALIFICATION_PASS\n'
                                    if defect == 'duplicate' else b'LONGHORN_FIRST_FAILURE phase=transport rc=1\n')
                if defect == 'missing-finalized':
                    (self.root / ('raw/' + stage + '/v1.finalized')).unlink()
                else:
                    self.write(name, value)
                    self.data['raw_evidence'][name] = sha(value)
                    self.data['stages'][stage]['raw'][name] = sha(value)
                with self.assertRaises((ValueError, FileNotFoundError)): self.consume()

    def test_raw_hash_path_and_frozen_source_are_verified(self):
        for name in ('../outside', '/tmp/outside'):
            with self.assertRaises(ValueError): reader.safe_path(self.root, name)
        path = self.root / 'raw/context.json'
        path.write_text('{}')
        with self.assertRaises(ValueError): self.consume()

    def test_frozen_verifier_changes_and_symlink_raw_are_rejected(self):
        source, _ = reader.VERIFIERS['original777']
        path = self.archives['original777'] / 'frozen-source' / source
        path.write_text('changed verifier')
        with self.assertRaises(ValueError): self.consume()
        path.write_bytes((SOURCE / source).read_bytes())
        log = self.root / ('raw/' + reader.STAGES[0] + '/' + reader.STAGES[0] + '.log')
        target = log.with_suffix('.original')
        log.rename(target)
        log.symlink_to(target)
        with self.assertRaises(ValueError): self.consume()

    def test_fixed_route_is_local_durable_and_static_registered(self):
        runner = (ROOT / 'tests/helpers/longhorn-runner.sh').read_text()
        self.assertIn('--longhorn-ownership-evidence-import) longhorn_local_gate ownership-evidence LONGHORN_OWNERSHIP_PASS', runner)
        branch = runner[runner.index('    ownership-evidence)'):runner.index('    static)')]
        self.assertIn('run-durable-gate.sh', branch)
        self.assertNotIn('ssh', branch)
        self.assertNotIn('longhorn_launch', branch)
        self.assertIn('test_longhorn_ownership_evidence', (ROOT / 'tests/helpers/longhorn-static.sh').read_text())


if __name__ == '__main__':
    unittest.main()
