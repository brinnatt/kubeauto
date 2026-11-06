"""Completed .2 adoption must prove every historical and current input edge."""
import copy
import hashlib
import importlib.util
import json
import shutil
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALL = load('longhorn-v2-install.py')
REVISION = load('longhorn-im-revision-adopt.py')
NAME = REVISION.SOURCE
COMPATIBILITY = load('longhorn-install-input-compatibility.py')


class RevisionEvidenceContracts(unittest.TestCase):
    def fixture(self, base, profile='v2'):
        def write(name, value):
            path = base/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value) if isinstance(value, dict) else value)
            return hashlib.sha256(path.read_bytes()).hexdigest()
        names = set(REVISION.CORE_INPUTS) | {'playbooks/09.longhorn.yml', 'tests/helpers/longhorn-install-input-compatibility.py'}
        for name in names:
            write(name, (ROOT/name).read_text() if name in (REVISION.DRIVER, 'tests/helpers/longhorn-install-input-compatibility.py') else 'published .2 input\n')
        write(REVISION.CATALOG, {'1.12.1': {'longhorn-instance-manager': REVISION.NEW_TAG}})
        identity = {'cluster_uid': profile, 'namespace_uid': 'namespace', 'node_uids': {'node': 'uid'}}
        baseline = {'baseline_identity': identity, 'native_block_receipt_sha256': 'retained-block-receipt'}
        nodes = ('longhorn-node-149',) if profile == 'v2-dr-target' else ('longhorn-node-146', 'longhorn-node-147', 'longhorn-node-148')
        managers = lambda tag: {node: {'uid': tag+'-'+node, 'node': node, 'image': 'registry/longhorn-instance-manager:'+tag} for node in nodes}
        current = {name: hashlib.sha256((base/name).read_bytes()).hexdigest() for name in names
                   if name != 'tests/helpers/longhorn-install-input-compatibility.py'}
        prior = {'status': 'pass', 'rc': 0, 'failure_markers': 0, 'durable': {'exit': 0, 'finalized': 0},
            'signed_scope': profile+'-im-adopt-qualification', 'version': '1.12.1',
            'instance_manager_version': REVISION.OLD_TAG, 'baseline_identity': identity,
            'remote_input_verification': {'mismatches': 0},
            'independent_readback': {'installation_healthy': True, 'empty_business': True, 'adoption_journal_absent': True,
                'baseline': baseline, 'managers': managers(REVISION.OLD_TAG)},
            'frozen_inputs': dict(current, **{NAME: 'accepted-.1-sha'}),
            'input_compatibility': {NAME: {'original_sha256': 'original-sha', 'sha256': 'accepted-.1-sha'}}}
        previous = 'tests/evidence/longhorn-'+profile+'-im-adopt-qualification-20261021.json'
        publication = {'status': 'pass', 'durable_rc': 0, 'failure_markers': 0, 'selected_image_count': 1,
            'image': 'brinnatt/longhorn-instance-manager:'+REVISION.NEW_TAG,
            'publication_record': {'key': 'brinnatt/longhorn-instance-manager:'+REVISION.NEW_TAG,
                'digest': 'sha256:'+'a'*64, 'platforms': {'linux/amd64': 'amd', 'linux/arm64': 'arm'}}}
        pub = 'tests/evidence/fixture-publication.json'
        selected = {'previous_evidence': {'path': previous, 'sha256': write(previous, prior)},
            'inputs': {name: {'old_sha256': prior['frozen_inputs'][name], 'sha256': sha,
                'source': 'official source + accepted .1 evidence', 'impact': 'Bounded reviewed integration'} for name, sha in current.items()}}
        plan = {'schema': 1, 'old_tag': REVISION.OLD_TAG, 'new_tag': REVISION.NEW_TAG,
            'publication_evidence': {'path': pub, 'sha256': write(pub, publication)}, 'profiles': {profile: selected}}
        plan_sha = write(REVISION.PLAN, plan)
        accepted = copy.deepcopy(prior)
        accepted.update(signed_scope=profile+'-im-revision-2-adopt-qualification',
            instance_manager_version=REVISION.NEW_TAG, frozen_inputs=current,
            plan_sha256=plan_sha, transition={'plan': {'path': REVISION.PLAN, 'sha256': plan_sha},
                'previous_evidence': selected['previous_evidence'], 'publication_evidence': plan['publication_evidence'],
                'old_tag': REVISION.OLD_TAG, 'new_tag': REVISION.NEW_TAG})
        accepted['independent_readback']['managers'] = managers(REVISION.NEW_TAG)
        # This deliberately wrong top-level value must never be consumed.
        prior['native_block_receipt_sha256'] = accepted['native_block_receipt_sha256'] = 'not-the-native-baseline'
        selected['previous_evidence']['sha256'] = write(previous, prior)
        accepted['transition']['previous_evidence'] = selected['previous_evidence']
        plan_sha = write(REVISION.PLAN, plan)
        accepted['plan_sha256'] = accepted['transition']['plan']['sha256'] = plan_sha
        accepted_path = 'tests/evidence/longhorn-'+profile+'-im-revision-2-adopt-qualification-20261021.json'
        write(accepted_path, accepted)
        evidence = {'baseline_identity': identity, 'frozen_inputs': {NAME: 'original-sha'}}
        return evidence, accepted, plan, prior, accepted_path, write

    def check(self, base, evidence, name=NAME):
        with patch.object(INSTALL, 'BASE', base):
            return INSTALL.qualification_input_current(evidence, name)

    def test_exact_completed_profiles_preserve_all_historical_files(self):
        for profile in ('v2', 'v2-dr-target'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); evidence, accepted, plan, prior, path, _ = self.fixture(base, profile)
                files = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()}
                self.assertTrue(self.check(base, evidence))
                self.assertEqual(files, {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts})
                self.assertNotEqual(prior['native_block_receipt_sha256'], prior['independent_readback']['baseline']['native_block_receipt_sha256'])

    def test_pending_failed_unclean_foreign_or_wrong_runtime_evidence_never_passes(self):
        defects = ('status', 'rc', 'failure_markers', 'durable-exit', 'durable-finalized', 'scope', 'version', 'tag',
            'remote', 'health', 'busy', 'journal', 'identity', 'readback-identity', 'native-receipt', 'no-native-receipt',
            'manager-tag', 'manager-node', 'manager-count', 'manager-uid', 'manager-old-uid', 'no-managers', 'new-input', 'extra-input', 'missing-input',
            'plan-sha', 'transition-plan', 'transition-prior', 'transition-publication', 'transition-old', 'transition-new')
        for defect in defects:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); evidence, accepted, _, _, path, write = self.fixture(base)
                readback = accepted['independent_readback']
                if defect in ('status', 'rc', 'failure_markers'): accepted[defect] = 'pending' if defect == 'status' else 1
                elif defect.startswith('durable-'): accepted['durable'][defect.split('-')[1]] = 1
                elif defect == 'scope': accepted['signed_scope'] = 'v2-im-adopt-qualification'
                elif defect == 'version': accepted['version'] = '1.12.0'
                elif defect == 'tag': accepted['instance_manager_version'] = REVISION.OLD_TAG
                elif defect == 'remote': accepted['remote_input_verification']['mismatches'] = 1
                elif defect in ('health', 'busy', 'journal'):
                    readback[{'health': 'installation_healthy', 'busy': 'empty_business', 'journal': 'adoption_journal_absent'}[defect]] = False
                elif defect == 'identity': evidence['baseline_identity'] = {'cluster_uid': 'foreign'}
                elif defect == 'readback-identity': readback['baseline']['baseline_identity'] = {'cluster_uid': 'foreign'}
                elif defect == 'native-receipt': readback['baseline']['native_block_receipt_sha256'] = accepted['native_block_receipt_sha256']
                elif defect == 'no-native-receipt': readback['baseline'].pop('native_block_receipt_sha256')
                elif defect == 'manager-tag': next(iter(readback['managers'].values()))['image'] = 'registry/mutable'
                elif defect == 'manager-uid': next(iter(readback['managers'].values()))['uid'] = ''
                elif defect == 'manager-old-uid':
                    row = next(iter(readback['managers'].values())); row['uid'] = REVISION.OLD_TAG+'-'+row['node']
                elif defect == 'manager-node': next(iter(readback['managers'].values()))['node'] = 'foreign'
                elif defect == 'manager-count': readback['managers']['duplicate'] = copy.deepcopy(next(iter(readback['managers'].values())))
                elif defect == 'no-managers': readback['managers'] = {}
                elif defect == 'new-input': accepted['frozen_inputs'][NAME] = 'other'
                elif defect == 'extra-input': accepted['frozen_inputs']['unreviewed'] = 'other'
                elif defect == 'missing-input': accepted['frozen_inputs'].pop(REVISION.CATALOG)
                elif defect == 'plan-sha': accepted['plan_sha256'] = 'other'
                elif defect.startswith('transition-'):
                    field = {'plan': 'plan', 'prior': 'previous_evidence', 'publication': 'publication_evidence',
                        'old': 'old_tag', 'new': 'new_tag'}[defect.removeprefix('transition-')]
                    accepted['transition'][field] = 'other'
                write(path, accepted)
                self.assertFalse(self.check(base, evidence))

    def test_prior_plan_publication_or_unrelated_current_input_drift_is_refused(self):
        for defect in ('prior-bytes', 'plan-bytes', 'publication-bytes', 'publication-arm64', 'input-bytes', 'old-input',
                       'prior-frozen', 'prior-compatibility', 'profile-missing', 'prior-path'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); evidence, accepted, plan, prior, path, write = self.fixture(base)
                selected = plan['profiles']['v2']
                if defect == 'prior-bytes': write(selected['previous_evidence']['path'], dict(prior, status='pending'))
                elif defect == 'publication-bytes': write(plan['publication_evidence']['path'], 'changed')
                elif defect == 'input-bytes': write('playbooks/09.longhorn.yml', 'unreviewed')
                else:
                    if defect == 'plan-bytes': plan['new_tag'] = 'mutable'
                    elif defect == 'old-input': selected['inputs'][NAME]['old_sha256'] = 'other'
                    elif defect == 'prior-frozen':
                        prior['frozen_inputs'][NAME] = 'other'
                        selected['previous_evidence']['sha256'] = write(selected['previous_evidence']['path'], prior)
                    elif defect == 'prior-compatibility':
                        prior['input_compatibility'][NAME]['original_sha256'] = 'other'
                        selected['previous_evidence']['sha256'] = write(selected['previous_evidence']['path'], prior)
                    elif defect == 'profile-missing': plan['profiles'] = {}
                    elif defect == 'prior-path': selected['previous_evidence']['path'] = 'tests/evidence/other.json'
                    elif defect == 'publication-arm64':
                        pub = json.loads((base/plan['publication_evidence']['path']).read_text())
                        del pub['publication_record']['platforms']['linux/arm64']
                        plan['publication_evidence']['sha256'] = write(plan['publication_evidence']['path'], pub)
                    sha = write(REVISION.PLAN, plan)
                    accepted['plan_sha256'] = accepted['transition']['plan']['sha256'] = sha
                    accepted['transition']['previous_evidence'] = selected['previous_evidence']
                    accepted['transition']['publication_evidence'] = plan['publication_evidence']
                    write(path, accepted)
                self.assertFalse(self.check(base, evidence))

    def test_earlier_compatibility_edge_requires_exact_original_intermediate_and_proof(self):
        for defect in ('none', 'original', 'intermediate', 'proof', 'symlink', 'traversal'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); evidence, _, _, _, _, write = self.fixture(base)
                sha = write('tests/evidence/prior-impact.json', 'accepted compatibility proof')
                evidence['frozen_inputs'][NAME] = 'earliest'
                row = {'original_sha256': 'earliest', 'sha256': 'original-sha',
                    'evidence': 'tests/evidence/prior-impact.json', 'evidence_sha256': sha}
                evidence['compatible_inputs'] = {NAME: row}
                if defect == 'original': row['original_sha256'] = 'foreign'
                elif defect == 'intermediate': row['sha256'] = 'foreign'
                elif defect == 'proof': write(row['evidence'], 'changed')
                elif defect == 'symlink':
                    p = base/row['evidence']; p.rename(base/'other'); p.symlink_to(base/'other')
                elif defect == 'traversal': row['evidence'] = '../foreign'
                self.assertEqual(self.check(base, evidence), defect == 'none')

    def test_exact_prior_frozen_input_needs_no_fabricated_compatibility_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); evidence, _, _, _, _, _ = self.fixture(base)
            evidence['frozen_inputs'][NAME] = 'accepted-.1-sha'
            self.assertTrue(self.check(base, evidence))
            evidence['frozen_inputs'][NAME] = 'unbound'
            self.assertFalse(self.check(base, evidence))

    def test_missing_symlink_or_malformed_new_evidence_does_not_grant_exception(self):
        for defect in ('missing', 'symlink', 'malformed', 'input-symlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); evidence, _, _, _, name, _ = self.fixture(base)
                path = base/name
                if defect == 'missing': path.unlink()
                elif defect == 'malformed': path.write_text('{invalid')
                else:
                    if defect == 'input-symlink': path = base/NAME
                    path.rename(base/'other'); path.symlink_to(base/'other')
                self.assertFalse(self.check(base, evidence))

    def reviewed_install_inputs(self, base):
        # Exact original paths/bytes for the old .2 cache/Event context. These
        # fixtures are inputs, not an allowance for current full/.3 source.
        inputs = {
            'tests/helpers/longhorn-v2-install.py':
                '7afb928fe787e6df4e18ccf41b7e1107febb1e2f4c8df4acc0ebe153bd75b84d',
            'tests/helpers/longhorn-v2-dr-target-install.py':
                'af065aa3959afe9158f9ef3f766e383dc59a58753ff4b069c1dc082b635c10e3',
            'tests/helpers/longhorn-v2-upgrade-baseline.py':
                '59b57a74d64624576feba26d155e336da8288b0c8ef4237852c7d06a18bd6d87',
        }
        for name, expected in inputs.items():
            fixture = ROOT/'tests/fixtures'/(Path(name).stem+'-reviewed-'+expected[:8]+'.py.txt')
            self.assertEqual(expected, hashlib.sha256(fixture.read_bytes()).hexdigest())
            shutil.copyfile(fixture, base/name)
        self.assertEqual(inputs['tests/helpers/longhorn-v2-install.py'],
                         COMPATIBILITY.INPUTS['tests/helpers/longhorn-v2-install.py'][1])
        return inputs

    def actual_inputs(self, base, *, reviewed_install=True):
        plan = json.loads((ROOT/REVISION.PLAN).read_text())
        names = {REVISION.PLAN, plan['publication_evidence']['path'], COMPATIBILITY.CONTRACT[0],
            'tests/helpers/longhorn-install-input-compatibility.py',
            'tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json',
            'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json'}
        for profile in ('v2', 'v2-dr-target'):
            names.update(plan['profiles'][profile]['inputs'])
            names.add(plan['profiles'][profile]['previous_evidence']['path'])
            names.add('tests/evidence/longhorn-'+profile+'-im-revision-2-adopt-qualification-20261021.json')
        names.add(COMPATIBILITY.FILESYSTEM_REVIEW[0])
        original = json.loads((ROOT/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').read_text())
        names.update(row['evidence'] for row in original.get('compatible_inputs', {}).values())
        for name in names:
            path = base/name; path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/name, path)
        # Reconstruct the frozen .2 input, never substitute today's global
        # configuration or weaken the live evidence reader's hash checks.
        frozen_product_inputs = {
            'conf/config.yml': 'longhorn-im-revision-2-config-be4e0410.yml.txt',
            'service/cluster/manager.py': 'longhorn-im-revision-2-cluster-manager-ee46940e.py.txt',
            'service/cluster/downloader.py': 'longhorn-im-revision-2-downloader-b66cf3de.py.txt',
            'service/cluster/registry.py': 'longhorn-im-revision-2-registry-89f06daf.py.txt',
        }
        for name, filename in frozen_product_inputs.items():
            fixture = ROOT/'tests/fixtures'/filename
            expected = plan['profiles']['v2']['inputs'][name]['sha256']
            self.assertEqual(expected, hashlib.sha256(fixture.read_bytes()).hexdigest())
            shutil.copyfile(fixture, base/name)
        # This immutable cohort must not reconstruct old bytes from today's
        # constants: adding a default-disabled middleware is unrelated to its
        # historical receipt. Keep the exact already-bound transition input.
        fixture = ROOT/'tests/fixtures/longhorn-im-revision-2-constants-fd9ae3d9.py.txt'
        transition = json.loads((ROOT/'tests/fixtures/longhorn-manager-pending-pin-transition.json').read_text())
        self.assertEqual(transition['inputs']['common/constants.py']['sha256'],
                         hashlib.sha256(fixture.read_bytes()).hexdigest())
        shutil.copyfile(fixture, base/'common/constants.py')
        # The immutable .2 cohort predates nullable disk-status handling. Its
        # exact populated-state source remains an isolated historical fixture.
        path = base/REVISION.SOURCE
        historical = path.read_text()
        for variable, key, count in (('status', 'name', 1), ('state', 'disk_name', 2)):
            current = variable+" = (current.get('status', {}).get('diskStatus') or {}).get("+key+") or {}"
            prior = variable+" = current.get('status', {}).get('diskStatus', {}).get("+key+", {})"
            self.assertEqual(count, historical.count(current))
            historical = historical.replace(current, prior)
        self.assertEqual(COMPATIBILITY.FILESYSTEM_INPUT[1][1],
                         hashlib.sha256(historical.encode()).hexdigest())
        path.write_text(historical)
        # This suite verifies the immutable .2 receipt chain. Current .3 pins
        # belong to the separate .3 adoption suite, not this historical fixture.
        # Restore only the IM and Manager pins and prove the complete old bytes.
        for name in ('common/constants.py', REVISION.CATALOG):
            path = base/name
            historical = path.read_bytes()
            manager_pin = (b'tags["longhorn-manager"] = "v1.12.1-kubeauto.2"' if name == 'common/constants.py'
                           else b'"longhorn-manager": "v1.12.1-kubeauto.2"')
            if manager_pin in historical:
                transition = json.loads((ROOT/'tests/fixtures/longhorn-manager-pending-pin-transition.json').read_text())
                edge = transition['inputs'][name]
                self.assertEqual(edge['sha256'], hashlib.sha256(historical).hexdigest())
                self.assertEqual(1, historical.count(manager_pin))
                historical = historical.replace(manager_pin, manager_pin.replace(b'kubeauto.2', b'kubeauto.1'), 1)
                self.assertEqual(edge['old_sha256'], hashlib.sha256(historical).hexdigest())
            historical = historical.replace(b'v1.12.1-kubeauto.3', b'v1.12.1-kubeauto.2')
            if name == 'common/constants.py':
                historical = historical.replace(b'            tags["longhorn-manager"] = "v1.12.1-kubeauto.1"\n', b'', 1)
            else:
                historical = historical.replace(b'"longhorn-manager": "v1.12.1-kubeauto.1"',
                                                b'"longhorn-manager": "v1.12.1"', 1)
            self.assertEqual(plan['profiles']['v2']['inputs'][name]['sha256'],
                             hashlib.sha256(historical).hexdigest())
            path.write_bytes(historical)
        # This immutable .2 fixture predates the parameterless retirement RPC
        # correction. Restore its exact old bytes, never widen .2 runtime reuse.
        name = 'roles/longhorn/files/block-retire.py'; path = base/name
        historical = path.read_bytes().replace(
            b"{'jsonrpc':'2.0','id':index,'method':method}",
            b"{'jsonrpc':'2.0','id':index,'method':method,'params':{}}", 1)
        self.assertEqual(plan['profiles']['v2']['inputs'][name]['sha256'],
                         hashlib.sha256(historical).hexdigest())
        path.write_bytes(historical)
        # The .2 contract consumes historical uninstall semantics; current GC
        # waiting and .3 retirement recovery are qualified by their owning suite.
        fixture = ROOT/'tests/fixtures/longhorn-lab-reviewed-ed237430.py.txt'
        self.assertEqual(COMPATIBILITY.INPUTS['tests/helpers/longhorn-lab.py'][1],
                         hashlib.sha256(fixture.read_bytes()).hexdigest())
        shutil.copyfile(fixture, base/'tests/helpers/longhorn-lab.py')
        # The .2 default-reader proof also binds its original upgrade reader.
        fixture = ROOT/'tests/fixtures/longhorn-v2-upgrade-baseline-reviewed-dcaef1fb.py.txt'
        self.assertEqual(COMPATIBILITY.CURRENT_INPUTS['tests/helpers/longhorn-v2-upgrade-baseline.py'][1],
                         hashlib.sha256(fixture.read_bytes()).hexdigest())
        shutil.copyfile(fixture, base/'tests/helpers/longhorn-v2-upgrade-baseline.py')
        if reviewed_install:
            self.reviewed_install_inputs(base)
            with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                COMPATIBILITY.read_completed_plan(REVISION, 'v2')
        return original

    def test_historical_fixture_rejects_configuration_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); self.actual_inputs(base)
            path = base/'conf/config.yml'
            path.write_bytes(path.read_bytes()+b'\n# unrelated configuration change\n')
            with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                with self.assertRaisesRegex(ValueError, 'conf/config.yml'):
                    COMPATIBILITY.read_completed_plan(REVISION, 'v2')

    def test_historical_fixture_keeps_old_retirement_rpc_and_refuses_current_product(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); self.actual_inputs(base)
            name = 'roles/longhorn/files/block-retire.py'; path = base/name
            plan = json.loads((base/REVISION.PLAN).read_text())
            self.assertEqual(plan['profiles']['v2']['inputs'][name]['sha256'],
                             hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertNotEqual(path.read_bytes(), (ROOT/name).read_bytes())
            path.write_bytes((ROOT/name).read_bytes())
            with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                with self.assertRaisesRegex(ValueError, 'block-retire.py'):
                    COMPATIBILITY.read_completed_plan(REVISION, 'v2')

    def test_historical_fixture_cannot_adopt_current_nullable_status_product(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); self.actual_inputs(base)
            (base/REVISION.SOURCE).write_bytes((ROOT/REVISION.SOURCE).read_bytes())
            with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                with self.assertRaisesRegex(ValueError, 'longhorn.py'):
                    COMPATIBILITY.read_completed_plan(REVISION, 'v2')

    def test_historical_fixture_rejects_current_revision3_product_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.actual_inputs(base)
            for name in ('common/constants.py', REVISION.CATALOG):
                path = base/name
                historical = path.read_bytes()
                self.assertIn(b'v1.12.1-kubeauto.2', historical)
                self.assertNotIn(b'v1.12.1-kubeauto.3', historical)
                path.write_bytes(historical.replace(b'v1.12.1-kubeauto.2', b'v1.12.1-kubeauto.3'))
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    with self.assertRaisesRegex(ValueError, name):
                        COMPATIBILITY.read_completed_plan(REVISION, 'v2')
                path.write_bytes(historical)

    def test_historical_revision2_reader_cannot_adopt_the_new_manager_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); self.actual_inputs(base)
            for name in ('common/constants.py', REVISION.CATALOG):
                path = base/name; historical = path.read_text()
                current = (historical+'            tags["longhorn-manager"] = "v1.12.1-kubeauto.1"\n'
                           if name == 'common/constants.py' else historical.replace(
                               '"longhorn-manager": "v1.12.1"', '"longhorn-manager": "v1.12.1-kubeauto.1"', 1))
                self.assertNotEqual(historical, current); path.write_text(current)
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    with self.assertRaisesRegex(ValueError, name): COMPATIBILITY.read_completed_plan(REVISION, 'v2')
                path.write_text(historical)

    def test_actual_completed_plan_uses_only_exact_reviewed_transitions(self):
        for profile in ('v2', 'v2-dr-target'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); self.actual_inputs(base, reviewed_install=False)
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    with self.assertRaisesRegex(ValueError, 'compatibility input changed: tests/helpers/longhorn-v2-install.py'):
                        COMPATIBILITY.read_completed_plan(REVISION, profile)
                inputs = self.reviewed_install_inputs(base)
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    COMPATIBILITY.read_completed_plan(REVISION, profile)
                    for name in inputs:
                        historical = (base/name).read_bytes()
                        (base/name).write_bytes((ROOT/name).read_bytes())
                        with self.assertRaisesRegex(ValueError, name):
                            COMPATIBILITY.read_completed_plan(REVISION, profile)
                        (base/name).write_bytes(historical)
                before = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()}
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    with self.assertRaisesRegex(ValueError, 'roles/longhorn/files/longhorn.py'):
                        REVISION.read_plan(profile)
                    COMPATIBILITY.read_completed_plan(REVISION, profile)
                    with self.assertRaisesRegex(ValueError, 'roles/longhorn/files/longhorn.py'):
                        REVISION.qualification(profile)
                self.assertEqual(before, {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()})

    def test_actual_completed_compatibility_refuses_unknown_input_or_contract_drift(self):
        defects = ('lab', 'install', 'contract', 'unrelated', 'plan', 'symlink', 'missing', 'profile',
                   'event-proof', 'event-proof-symlink', 'event-proof-missing',
                   'filesystem-review', 'filesystem-review-symlink', 'filesystem-review-missing')
        for defect in defects:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); self.actual_inputs(base)
                names = {'lab': 'tests/helpers/longhorn-lab.py', 'install': 'tests/helpers/longhorn-v2-install.py',
                         'contract': COMPATIBILITY.CONTRACT[0], 'unrelated': REVISION.SOURCE, 'plan': REVISION.PLAN,
                         'event-proof': 'tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json',
                         'event-proof-symlink': 'tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json',
                         'event-proof-missing': 'tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json',
                         'filesystem-review': COMPATIBILITY.FILESYSTEM_REVIEW[0],
                         'filesystem-review-symlink': COMPATIBILITY.FILESYSTEM_REVIEW[0],
                         'filesystem-review-missing': COMPATIBILITY.FILESYSTEM_REVIEW[0],
                         'symlink': 'tests/helpers/longhorn-lab.py', 'missing': 'tests/helpers/longhorn-v2-install.py'}
                if defect != 'profile':
                    path = base/names[defect]
                    if defect in ('symlink', 'event-proof-symlink', 'filesystem-review-symlink'):
                        raw = path.read_bytes(); path.unlink(); other = base/'other'; other.write_bytes(raw); path.symlink_to(other)
                    elif defect in ('missing', 'event-proof-missing', 'filesystem-review-missing'): path.unlink()
                    else: path.write_bytes(path.read_bytes()+b'\n# unreviewed change\n')
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        COMPATIBILITY.read_completed_plan(REVISION, 'v1' if defect == 'profile' else 'v2')

    def test_completed_compatibility_missing_or_changed_mapping_never_grants_exception(self):
        for defect in ('missing', 'old-sha', 'current-sha'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); self.actual_inputs(base)
                pairs = copy.deepcopy(COMPATIBILITY.INPUTS)
                name = 'tests/helpers/longhorn-lab.py'; old, current = pairs[name]
                if defect == 'missing': del pairs[name]
                else: pairs[name] = ('foreign', current) if defect == 'old-sha' else (old, 'foreign')
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base), \
                        patch.object(COMPATIBILITY, 'INPUTS', pairs):
                    with self.assertRaises(ValueError): COMPATIBILITY.read_completed_plan(REVISION, 'v2')

    def test_real_read_plan_to_volume_baseline_failure_and_compatibility_success(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); evidence = self.actual_inputs(base, reviewed_install=False)
            work = base/'cluster'; receipt = work/'yml/longhorn/block-disks.json'; receipt.parent.mkdir(parents=True)
            nodes = {name: {'name': name, 'longhorn_node_uid': uid, 'disks': {'disk': {'disk_uuid': 'disk-'+name}}}
                     for name, uid in evidence['baseline_identity']['node_uids'].items()}
            receipt.write_text(json.dumps({'nodes': nodes}))
            evidence['native_block_receipt_sha256'] = hashlib.sha256(receipt.read_bytes()).hexdigest()
            (base/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').write_text(json.dumps(evidence))
            def get(kind, name=None, namespace=None):
                if kind == 'nodes.longhorn.io':
                    row = nodes[name]
                    return {'metadata': {'uid': row['longhorn_node_uid']}, 'status': {'diskStatus': {'disk': {'diskUUID': row['disks']['disk']['disk_uuid']}}}}
                if kind == 'settings.longhorn.io': return {'value': 'true' if name == 'v2-data-engine' else 'false'}
                if kind == 'instancemanagers.longhorn.io': return {'items': [{'spec': {'dataEngine': 'v2'}} for _ in nodes]}
                return {'items': []}
            lab = SimpleNamespace(DIRECTORY=work, owner=lambda: evidence['baseline_identity'], get=get,
                                  installation_health=lambda: None)
            with patch.object(INSTALL, 'BASE', base):
                with self.assertRaisesRegex(ValueError, 'compatibility input changed: tests/helpers/longhorn-v2-install.py'):
                    INSTALL.volume_baseline(lab)
            self.reviewed_install_inputs(base)
            spec = importlib.util.spec_from_file_location('reviewed_install', base/'tests/helpers/longhorn-v2-install.py')
            install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
            original_load = install.load
            strict = SimpleNamespace(read_completed_plan=lambda revision, profile: revision.read_plan(profile),
                                     input_current=COMPATIBILITY.input_current)
            def strict_load(name):
                return strict if name == 'longhorn-install-input-compatibility.py' else original_load(name)
            before = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
            with patch.object(install, 'BASE', base):
                with patch.object(install, 'load', side_effect=strict_load):
                    with self.assertRaisesRegex(ValueError, 'revision plan input changed.*roles/longhorn/files/longhorn.py'):
                        install.volume_baseline(lab)
                install.volume_baseline(lab)
                changed_files = {'lab-helper': 'tests/helpers/longhorn-lab.py',
                    'event-proof': 'tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json',
                    'event-proof-symlink': 'tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json',
                    'event-proof-missing': 'tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json'}
                for defect in ('namespace', 'disk', 'business') + tuple(changed_files):
                    with self.subTest(defect=defect):
                        if defect == 'namespace':
                            lab.owner = lambda: dict(evidence['baseline_identity'], namespace_uid='foreign')
                        elif defect == 'disk': nodes[next(iter(nodes))]['disks']['disk']['disk_uuid'] = 'foreign'
                        elif defect == 'business': (work/'longhorn-install-business.json').write_text('{}')
                        else:
                            path = base/changed_files[defect]; content = path.read_bytes()
                            if defect == 'event-proof-missing': path.unlink()
                            elif defect == 'event-proof-symlink':
                                other = base/'foreign-proof'; other.write_bytes(content)
                                path.unlink(); path.symlink_to(other)
                            else: path.write_bytes(content+b'\n')
                        with self.assertRaises(ValueError): install.volume_baseline(lab)
                        lab.owner = lambda: evidence['baseline_identity']
                        if defect == 'disk':
                            node = next(iter(nodes)); nodes[node]['disks']['disk']['disk_uuid'] = 'disk-'+node
                        if defect == 'business': (work/'longhorn-install-business.json').unlink()
                        if defect in changed_files:
                            path.unlink(missing_ok=True); path.write_bytes(content)
                            if defect == 'event-proof-symlink': other.unlink()
            self.assertEqual(before, {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts})



DEFAULT_READER_REVIEW_SOURCE = ROOT/'tests/evidence/longhorn-completed-default-reader-reuse-20261022.json'


class DefaultReaderReuseContracts(unittest.TestCase):
    def fixture(self, base):
        original = RevisionEvidenceContracts().actual_inputs(base, reviewed_install=False)
        review = 'tests/evidence/longhorn-completed-default-reader-reuse-20261022.json'
        target = base/review
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_READER_REVIEW_SOURCE, target)
        shutil.copyfile(Path(COMPATIBILITY.__file__), base/'tests/helpers/longhorn-install-input-compatibility.py')
        return original

    def test_current_default_source_and_target_reuse_all_original_guards_without_rewriting_evidence(self):
        for profile in ('v2', 'v2-dr-target'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                original = self.fixture(base)
                before = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()}
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    with self.assertRaisesRegex(ValueError, 'revision plan input changed'):
                        REVISION.read_plan(profile)  # New adoption still refuses the changed helpers.
                    COMPATIBILITY.read_completed_plan(REVISION, profile)
                if profile == 'v2':
                    self.assertTrue(RevisionEvidenceContracts().check(base, original))
                self.assertEqual(before, {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*')
                                         if p.is_file() and '__pycache__' not in p.parts})

    def test_current_default_reader_refuses_unknown_source_review_product_and_publication_drift(self):
        defects = ('source-reader', 'target-reader', 'upgrade-reader', 'review', 'missing-review',
                   'symlink-review', 'product', 'catalog', 'publication', 'event-proof')
        for defect in defects:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                self.fixture(base)
                plan = json.loads((base/REVISION.PLAN).read_text())
                names = {
                    'source-reader': 'tests/helpers/longhorn-v2-install.py',
                    'target-reader': 'tests/helpers/longhorn-v2-dr-target-install.py',
                    'upgrade-reader': 'tests/helpers/longhorn-v2-upgrade-baseline.py',
                    'review': 'tests/evidence/longhorn-completed-default-reader-reuse-20261022.json',
                    'missing-review': 'tests/evidence/longhorn-completed-default-reader-reuse-20261022.json',
                    'symlink-review': 'tests/evidence/longhorn-completed-default-reader-reuse-20261022.json',
                    'product': REVISION.SOURCE, 'catalog': REVISION.CATALOG,
                    'publication': plan['publication_evidence']['path'],
                    'event-proof': COMPATIBILITY.EVENT_RECOVERY[0],
                }
                path = base/names[defect]
                if defect == 'missing-review':
                    path.unlink()
                elif defect == 'symlink-review':
                    other = base/'foreign-review'
                    path.rename(other)
                    path.symlink_to(other)
                else:
                    path.write_bytes(path.read_bytes()+b'\n# unreviewed change\n')
                with patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        COMPATIBILITY.read_completed_plan(REVISION, 'v2')

    def test_current_default_reader_refuses_full_continuation_and_revision3_even_with_empty_or_broken_inputs(self):
        for context in ('full', 'empty-full', 'continuation', 'empty-continuation', 'revision3', 'broken-revision3'):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                self.fixture(base)
                env = {}
                if 'full' in context:
                    env['LONGHORN_FULL_EVIDENCE_DIR'] = '' if context.startswith('empty') else '/foreign'
                elif 'continuation' in context:
                    env['LONGHORN_V2_RUNTIME_CONTINUATION'] = '' if context.startswith('empty') else '/foreign'
                else:
                    path = base/'tests/evidence/longhorn-v2-im-revision-3-adopt-qualification-20261022.json'
                    if context == 'broken-revision3':
                        path.symlink_to(base/'missing')
                    else:
                        path.write_text('{}')
                with patch.dict('os.environ', env), patch.object(REVISION, 'BASE', base), patch.object(COMPATIBILITY, 'BASE', base):
                    with self.assertRaisesRegex(ValueError, 'excludes full/continuation and .3 adoption'):
                        COMPATIBILITY.read_completed_plan(REVISION, 'v2')


if __name__ == '__main__':
    unittest.main()
