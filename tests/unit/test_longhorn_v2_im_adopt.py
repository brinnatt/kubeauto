"""Narrow IM adoption: product diff, identity, cleanup and exit contracts."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('im_adopt_contract', ROOT/'tests/helpers/longhorn-v2-im-adopt.py')
ADOPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADOPT)


class AdoptContracts(unittest.TestCase):
    def test_fixed_runner_registration_and_static_preflight(self):
        runner = ROOT/'tests/helpers/longhorn-runner.sh'
        for engine in ADOPT.ENGINES:
            mode = engine+'-im-adopt-qualification'
            script = ('source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\n'
                      'longhorn_dispatch --longhorn-'+mode+'\n')
            result = subprocess.run(['bash', '-c', script, 'contract', str(runner)],
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, mode+' LONGHORN_'+engine.upper().replace('-', '_')+'_IM_ADOPT_QUALIFICATION_PASS')
            for variable in ('module', 'mode'):
                branches = re.findall(r'case "\$'+variable+r'" in ([^\n]+)', runner.read_text())
                self.assertEqual(len(branches), 1)
                self.assertIn(mode, branches[0].split(')')[0].split('|'))
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertRegex(live, r'v1-im-adopt-qualification\|v2-im-adopt-qualification\|v2-dr-target-im-adopt-qualification\)\s+exec bash "\$BASE/tests/helpers/longhorn-v2-im-adopt.sh" "\$MODE"')
        static = (ROOT/'tests/helpers/longhorn-static.sh').read_text()
        self.assertRegex(static, r'-m py_compile[^\n]*tests/helpers/longhorn-v2-im-adopt.py')
        self.assertRegex(static, r'-m unittest[^\n]*tests.unit.test_longhorn_v2_im_adopt')

    def test_old_checker_requires_exact_reviewed_product_diff_and_all_original_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = 'import json\n'+ADOPT.OLD_LINE
            current = original.replace(ADOPT.OLD_LINE, ADOPT.NEW_LINES)
            evidence = {'frozen_inputs': {ADOPT.SOURCE: hashlib.sha256(original.encode()).hexdigest()}}
            for name in (ADOPT.SOURCE, 'roles/longhorn/files/block-check.py',
                         'tests/helpers/longhorn-lab.py', 'tests/helpers/longhorn-v2-lab.py',
                         'tests/helpers/longhorn-v2-core.py'):
                path = base/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(current if name == ADOPT.SOURCE else 'fixed dependency\n')
                if name != ADOPT.SOURCE:
                    evidence['frozen_inputs'][name] = ADOPT.digest(path)
            catalog = base/'roles/longhorn/files/images.json'
            catalog.write_text(json.dumps({'1.12.1': {'longhorn-instance-manager': ADOPT.NEW_TAG}}))
            evidence['frozen_inputs']['roles/longhorn/files/images.json'] = hashlib.sha256(
                catalog.read_text().replace(ADOPT.NEW_TAG, ADOPT.OLD_TAG).encode()).hexdigest()
            with patch.object(ADOPT, 'BASE', base):
                self.assertEqual(ADOPT.previous_source(evidence), original)
                source = base/ADOPT.SOURCE
                source.write_text(current+'# unrelated mutation\n')
                with self.assertRaisesRegex(ValueError, 'unrelated'): ADOPT.previous_source(evidence)
                source.write_text(current)
                (base/'tests/helpers/longhorn-v2-core.py').write_text('changed')
                with self.assertRaisesRegex(ValueError, 'prerequisite changed'): ADOPT.previous_source(evidence)

    def test_old_checker_allows_only_the_bound_publication_inventory_reference_change(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = 'import json\n'+ADOPT.OLD_LINE
            old_inventory = 'tests/evidence/longhorn-images-20261019.json'
            new_inventory = 'tests/evidence/longhorn-images-20261021.json'
            old_lab = "production = json.loads((BASE / '"+old_inventory+"').read_text())['images']\n"
            new_lab = old_lab.replace(old_inventory, new_inventory)
            evidence = {'frozen_inputs': {}}
            for name in (ADOPT.SOURCE, 'roles/longhorn/files/block-check.py',
                         'tests/helpers/longhorn-lab.py', 'tests/helpers/longhorn-v2-lab.py',
                         'tests/helpers/longhorn-v2-core.py'):
                path = base/name
                path.parent.mkdir(parents=True, exist_ok=True)
                text = original if name == ADOPT.SOURCE else old_lab if name.endswith('/longhorn-lab.py') else 'fixed dependency\n'
                evidence['frozen_inputs'][name] = hashlib.sha256(text.encode()).hexdigest()
                path.write_text(text.replace(ADOPT.OLD_LINE, ADOPT.NEW_LINES) if name == ADOPT.SOURCE else text)
            catalog = base/'roles/longhorn/files/images.json'
            catalog.write_text(json.dumps({'1.12.1': {'longhorn-instance-manager': ADOPT.NEW_TAG}}))
            evidence['frozen_inputs']['roles/longhorn/files/images.json'] = hashlib.sha256(
                catalog.read_text().replace(ADOPT.NEW_TAG, ADOPT.OLD_TAG).encode()).hexdigest()
            lab = base/'tests/helpers/longhorn-lab.py'
            cases = {'unchanged': old_lab, 'publication-only': new_lab,
                     'extra-code': new_lab+'foreign_mutation()\n',
                     'duplicate-new-reference': new_lab+repr(new_inventory)+'\n',
                     'mixed-references': new_lab+repr(old_inventory)+'\n',
                     'wrong-inventory': old_lab.replace('20261019', '20261022')}
            with patch.object(ADOPT, 'BASE', base):
                for case, text in cases.items():
                    lab.write_text(text)
                    with self.subTest(case=case):
                        if case in ('unchanged', 'publication-only'):
                            self.assertEqual(ADOPT.previous_source(evidence), original)
                        else:
                            with self.assertRaisesRegex(ValueError, 'prerequisite changed'):
                                ADOPT.previous_source(evidence)
                lab.write_text(new_lab)
                evidence['frozen_inputs']['tests/helpers/longhorn-lab.py'] = 'foreign-sha'
                with self.assertRaisesRegex(ValueError, 'prerequisite changed'):
                    ADOPT.previous_source(evidence)

    def test_native_managers_require_new_image_three_nodes_and_default_setting(self):
        image = 'registry/longhorn-instance-manager:'+ADOPT.NEW_TAG
        rows = [{'metadata': {'name': 'im-'+str(i), 'uid': 'uid-'+str(i)},
                 'spec': {'nodeID': 'longhorn-node-'+str(i), 'dataEngine': 'v2', 'type': 'aio', 'image': image},
                 'status': {'currentState': 'running'}} for i in (146, 147, 148)]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        (base/'yml/longhorn').mkdir(parents=True)
        addresses = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
        (base/'yml/longhorn/input.json').write_text(json.dumps({'longhorn_v2_nodes': addresses}))
        lab = SimpleNamespace(NODES=addresses, DIRECTORY=base)
        for failure in ('none', 'image', 'node', 'engine', 'count', 'state', 'deleting', 'setting'):
            current = copy.deepcopy(rows)
            if failure in ('image', 'node', 'engine'):
                key = {'image': 'image', 'node': 'nodeID', 'engine': 'dataEngine'}[failure]
                current[0]['spec'][key] = 'foreign'
            if failure == 'count': current.pop()
            if failure == 'state': current[0]['status']['currentState'] = 'starting'
            if failure == 'deleting': current[0]['metadata']['deletionTimestamp'] = 'now'
            lab.get = lambda kind, *a, **kw: {'items': current} if kind.startswith('instancemanagers') else {'value': 'foreign' if failure == 'setting' else image}
            with self.subTest(failure=failure):
                if failure == 'none': self.assertEqual(len(ADOPT.managers(lab, image)), 3)
                else:
                    with self.assertRaises(ValueError): ADOPT.managers(lab, image)

    def test_failure_cleanup_does_not_adopt_foreign_or_setup_phase_business(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base/'config.yml'; config.write_text('original')
            row = {'run_id': 'owned', 'engine': 'v2', 'phase': 'setup-intent',
                   'before': {'config_sha256': ADOPT.digest(config), 'baseline_identity': {'namespace_uid': 'ns'}}}
            record = base/ADOPT.RECORD; record.write_text(json.dumps(row))
            lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'owned', 'namespace_uid': 'ns'},
                core_identity=MagicMock(), get=lambda *a: {'metadata': {'uid': 'ns'}},
                cleanup_business=MagicMock(), restore_request_input=MagicMock())
            with patch.object(ADOPT, 'empty') as empty:
                ADOPT.clean(lab, 2)
                lab.cleanup_business.assert_not_called()
                empty.assert_called_once_with(lab)
                self.assertTrue(record.exists())
                row['phase'] = 'business-intent'; record.write_text(json.dumps(row))
                ADOPT.clean(lab, 2)
                lab.cleanup_business.assert_called_once_with(relay_pid=2)
                row['run_id'] = 'foreign'; record.write_text(json.dumps(row))
                with self.assertRaisesRegex(ValueError, 'foreign journal'): ADOPT.clean(lab, 2)

    def test_v1_six_kubernetes_nodes_have_three_client_managers_and_filesystem_receipts(self):
        addresses = (135, 40, 72, 212, 165, 238)
        workers = ('192.168.122.212', '192.168.122.165', '192.168.122.238')
        nodes = []
        managers = []
        image = 'registry/im:'+ADOPT.NEW_TAG
        for n in addresses:
            worker = n in (212, 165, 238)
            if not worker:
                continue
            nodes.append({'metadata': {'name': 'longhorn-node-'+str(n), 'uid': 'node-'+str(n)},
                'spec': {'disks': {'data': {'diskType': 'filesystem', 'path': '/owned'}} if worker else {}},
                'status': {'diskStatus': {'data': {'diskUUID': 'disk-'+str(n)}} if worker else {}}})
            managers.append({'metadata': {'name': 'im-'+str(n), 'uid': 'im-'+str(n)},
                'spec': {'nodeID': 'longhorn-node-'+str(n), 'dataEngine': 'v1', 'type': 'aio', 'image': image},
                'status': {'currentState': 'running'}})
        state = {'node_uids': {'longhorn-node-'+str(n): 'uid-'+str(n) for n in addresses},
                 'disks': {n: {'uuid': 'fs-'+n, 'path': '/owned'} for n in workers}}
        def remote(node, argv, *, stdin):
            compile(argv[2], '<readonly-fs-probe>', 'exec')
            return SimpleNamespace(stdout=json.dumps({'receipt_sha256': 'a'*64, 'mount': {'uuid': node}}))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        (base/'yml/longhorn').mkdir(parents=True)
        (base/'yml/longhorn/input.json').write_text(json.dumps({'longhorn_client_nodes': workers,
            'longhorn_v2_nodes': []}))
        lab = SimpleNamespace(DIRECTORY=base, NODES=tuple('192.168.122.'+str(n) for n in addresses), WORKERS=workers,
            DATA='/owned', owner=lambda: state, remote=MagicMock(side_effect=remote),
            disk_parent_script=lambda: 'pass', disk_cleanup_script=lambda: 'pass')
        lab.get = lambda kind, *a, **kw: {'items': nodes if kind.startswith('nodes.') else managers} if not kind.startswith('settings.') else {'value': image}
        self.assertEqual(len(ADOPT.managers(lab, image, engine='v1')), 3)
        with self.assertRaises(ValueError): ADOPT.managers(lab, image, engine='v2')
        before = ADOPT.filesystem_disks(lab)
        self.assertEqual(set(before['host_receipts']), set(workers))
        self.assertEqual(len(before['native_nodes']), 3)
        self.assertEqual(lab.remote.call_count, 9)
        master = {'metadata': {'name': 'longhorn-node-135', 'uid': 'master'}, 'spec': {'disks': {}}}
        nodes.append(master)
        self.assertEqual(len(ADOPT.filesystem_disks(lab)['native_nodes']), 4)
        master['spec']['disks'] = {'foreign': {'path': '/foreign'}}
        with self.assertRaisesRegex(ValueError, 'allowlist'): ADOPT.filesystem_disks(lab)
        nodes.pop()
        for altered in (nodes[:-1], nodes+[copy.deepcopy(nodes[0])],
                        nodes+[{'metadata': {'name': 'longhorn-node-foreign'}, 'spec': {'disks': {}}}]):
            with self.subTest(nodes=altered), patch.object(lab, 'get', return_value={'items': altered}):
                with self.assertRaisesRegex(ValueError, 'topology'): ADOPT.filesystem_disks(lab)

    def test_required_nodes_follow_rendered_product_allowlists(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'yml/longhorn').mkdir(parents=True)
            path = base/'yml/longhorn/input.json'
            workers = ('192.168.122.212', '192.168.122.165', '192.168.122.238')
            v2 = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
            lab = SimpleNamespace(DIRECTORY=base, WORKERS=workers, NODES=v2)
            config = {'longhorn_client_nodes': workers, 'longhorn_v2_nodes': v2}
            path.write_text(json.dumps(config))
            self.assertEqual(ADOPT.required_nodes(lab, 'v1'), {'longhorn-node-'+n.rsplit('.', 1)[1] for n in workers})
            self.assertEqual(ADOPT.required_nodes(lab, 'v2'), {'longhorn-node-'+n.rsplit('.', 1)[1] for n in v2})
            path.write_text(json.dumps({'longhorn_v2_nodes': v2}))
            self.assertEqual(len(ADOPT.required_nodes(lab, 'v1')), 3)
            for clients in (workers+workers[:1], workers+('192.168.122.135', '192.168.122.40', '192.168.122.72'),
                            ('192.168.122.135', *workers[1:])):
                path.write_text(json.dumps(dict(config, longhorn_client_nodes=clients)))
                with self.assertRaisesRegex(ValueError, 'inventory'): ADOPT.required_nodes(lab, 'v1')

    def test_cache_uses_complete_publication_key_with_the_real_cache_helper(self):
        spec = importlib.util.spec_from_file_location('im_adopt_cache_fixture', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        manifest = json.dumps({'schemaVersion': 2})
        key = 'brinnatt/longhorn-instance-manager:'+ADOPT.NEW_TAG
        row = {'key': key, 'platforms': {'linux/amd64': 'sha256:'+hashlib.sha256(manifest.encode()).hexdigest()}}
        with patch.object(lab, 'owner'), patch.object(lab, 'publication_records', return_value=([row], [])), \
                patch.object(lab, 'execute', return_value=SimpleNamespace(returncode=0, stdout=manifest)) as execute:
            ADOPT.cache(lab)
            self.assertEqual(execute.call_args.args[0][-1], 'docker://127.0.0.1:5000/'+key)
            with self.assertRaisesRegex(ValueError, 'unknown selected'):
                lab.cache_artifacts({'longhorn-instance-manager:'+ADOPT.NEW_TAG})

    def test_v1_adoption_uses_independent_idempotence_and_healthy_readback_with_failed_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); (base/'tests/evidence').mkdir(parents=True)
            snapshot = base/'tests/evidence/longhorn-snapshot-20261020.json'
            snapshot.write_text(json.dumps({'baseline_identity': {'namespace_uid': 'original'}}))
            parent = {'status': 'pass', 'signed_scope': 'idempotence', 'case': 'LONGHORN-07', 'rc': 0, 'failure_markers': 0,
                'remote_input_verification': {'mismatches': 0}, 'baseline_identity': {'namespace_uid': 'original'},
                'independent_readback': {'installation_healthy': True, 'credential_uids_match_original_state': True,
                    'baseline_identity': {'namespace_uid': 'original'}},
                'parallel_parent': {'status': 'failed', 'rc': 1},
                'frozen_inputs': {'tests/evidence/longhorn-snapshot-20261020.json': ADOPT.digest(snapshot)}}
            path = base/ADOPT.V1_QUALIFICATION
            with patch.object(ADOPT, 'BASE', base):
                for failure in ('none', 'status', 'rc', 'failure', 'remote', 'readback', 'snapshot', 'signed_scope', 'case', 'healthy', 'credential', 'identity'):
                    row = copy.deepcopy(parent)
                    if failure == 'status': row['status'] = 'running'
                    if failure == 'rc': row['rc'] = 17
                    if failure == 'failure': row['failure_markers'] = 1
                    if failure == 'remote': row['remote_input_verification']['mismatches'] = 1
                    if failure == 'readback': row['independent_readback'] = None
                    if failure == 'snapshot': row['frozen_inputs']['tests/evidence/longhorn-snapshot-20261020.json'] = 'foreign'
                    if failure in ('signed_scope', 'case'): row[failure] = 'security'
                    if failure == 'healthy': row['independent_readback']['installation_healthy'] = False
                    if failure == 'credential': row['independent_readback']['credential_uids_match_original_state'] = False
                    if failure == 'identity': row['independent_readback']['baseline_identity']['namespace_uid'] = 'foreign'
                    path.write_text(json.dumps(row))
                    if failure == 'none':
                        self.assertEqual(ADOPT.qualification('v1')['baseline_identity'], {'namespace_uid': 'original'})
                    else:
                        with self.assertRaises(ValueError): ADOPT.qualification('v1')

    def test_v2_adoption_binds_accepted_partial_to_original_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'tests/evidence').mkdir(parents=True)
            original = {'status': 'pass', 'baseline_identity': {'namespace_uid': 'original-v2'}}
            (base/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').write_text(json.dumps(original))
            accepted = {'status': 'pass', 'signed_scope': 'v2-interrupt-manager', 'rc': 0, 'failure_markers': 0,
                        'remote_input_verification': {'mismatches': 0}, 'independent_readback': {'source': 'healthy'},
                        'parallel_parent': {'status': 'failed', 'rc': 1}}
            path = base/ADOPT.V2_QUALIFICATION
            with patch.object(ADOPT, 'BASE', base):
                path.write_text(json.dumps(accepted))
                self.assertEqual(ADOPT.qualification('v2'), original)
                path.write_text(json.dumps(dict(accepted, signed_scope='security')))
                with self.assertRaisesRegex(ValueError, 'scope'): ADOPT.qualification('v2')

    def test_adoption_uses_product_v1_default_only_when_engine_selection_is_omitted(self):
        omitted = object()
        for engine in ADOPT.ENGINES:
            for selected in (omitted, ['v1'], ['v2'], ['v1', 'v2'], [], None, 'v1'):
                with self.subTest(engine=engine, selected=selected), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    config = {'longhorn_version': '1.12.1', 'longhorn_image_prefix': 'registry'}
                    if selected is not omitted:
                        config['longhorn_data_engines'] = selected
                    path = base/'config.yml'
                    path.write_text(ADOPT.yaml.safe_dump(config))
                    original_config = path.read_bytes()
                    lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'owned'},
                        write_private=MagicMock(), run_requests=MagicMock(), business=MagicMock(),
                        wait=lambda label, timeout, predicate: predicate())
                    install = SimpleNamespace(health=MagicMock(), business=MagicMock())
                    old = {str(i): {'uid': 'old'+str(i)} for i in range(3)}
                    new = {str(i): {'uid': 'new'+str(i)} for i in range(3)}
                    accepts = (['v1'] if selected is omitted else selected) == ['v1' if engine == 'v1' else 'v2']
                    with patch.object(ADOPT, 'BASE', base), \
                            patch.object(ADOPT, 'qualification', return_value={'status': 'pass', 'rc': 0, 'failure_markers': 0}), \
                            patch.object(ADOPT, 'previous_source', return_value='old'), \
                            patch.object(ADOPT, 'baseline', return_value={'stable': True}), \
                            patch.object(ADOPT, 'old_health'), patch.object(ADOPT, 'digest', return_value='sha'), \
                            patch.object(ADOPT, 'managers', side_effect=[old, new, new]) as managers:
                        if accepts:
                            ADOPT.adopt(install, lab, 7, engine)
                            lab.run_requests.assert_called_once_with(7, [])
                            if engine == 'v2-dr-target':
                                install.business.assert_called_once_with(lab, 7)
                                lab.business.assert_not_called()
                            else:
                                lab.business.assert_called_once_with('install', relay_pid=7)
                            self.assertEqual('verified', lab.write_private.call_args.args[1]['phase'])
                        else:
                            with self.assertRaisesRegex(ValueError, 'single-engine'):
                                ADOPT.adopt(install, lab, 7, engine)
                            managers.assert_not_called()
                            lab.write_private.assert_not_called()
                            lab.run_requests.assert_not_called()
                            lab.business.assert_not_called()
                    self.assertEqual(original_config, path.read_bytes())

    def test_target_qualification_binds_its_own_installation_and_unchanged_target_helpers(self):
        original = json.loads((ROOT/ADOPT.TARGET_QUALIFICATION).read_text())
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base/ADOPT.TARGET_QUALIFICATION
            path.parent.mkdir(parents=True)
            dependencies = ('tests/helpers/longhorn-v2-dr-target.py', 'tests/helpers/longhorn-v2-dr-target-install.py')
            for name in dependencies:
                target = base/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT/name).read_bytes())
            # This is a synthetic accepted fixture for the helpers copied
            # above, not permission to rewrite or reuse historical evidence.
            fixture = copy.deepcopy(original)
            for name in dependencies:
                fixture['frozen_inputs'][name] = ADOPT.digest(base/name)
            for defect in ('none', 'status', 'rc', 'failure', 'remote', 'identity', 'source-node',
                           'receipt', 'disk-uuid', 'business', 'relay', 'replicas', 'cross-node', 'detached',
                           'helper', 'target-helper-bytes', 'install-helper-bytes'):
                with self.subTest(defect=defect):
                    accepted = copy.deepcopy(fixture)
                    for name in dependencies:
                        (base/name).write_bytes((ROOT/name).read_bytes())
                    if defect == 'status': accepted['status'] = 'running'
                    if defect == 'rc': accepted['rc'] = 1
                    if defect == 'failure': accepted['failure_markers'] = 1
                    if defect == 'remote': accepted['remote_input_verification']['mismatches'] = 1
                    if defect == 'identity': accepted['independent_readback']['baseline_identity']['cluster_uid'] = 'foreign'
                    if defect == 'source-node': accepted['baseline_identity']['raw_disks']['192.168.47.146'] = {}
                    if defect == 'receipt': accepted['independent_readback']['native_block_receipt_sha256'] = 'foreign'
                    if defect == 'disk-uuid': accepted['independent_readback']['previous_native_uuid_preserved'] = False
                    if defect == 'business': accepted['independent_readback']['remaining_business'] = 1
                    if defect == 'relay': accepted['independent_readback']['relays'] = 1
                    if defect == 'replicas': accepted['business']['replicas'] = 3
                    if defect == 'cross-node': accepted['business']['cross_node'] = True
                    if defect == 'detached': accepted['business']['actual_detached'] = False
                    if defect == 'helper': accepted['frozen_inputs'][dependencies[0]] = 'foreign'
                    if defect in ('target-helper-bytes', 'install-helper-bytes'):
                        name = dependencies[0 if defect == 'target-helper-bytes' else 1]
                        with (base/name).open('ab') as stream: stream.write(b'\n# foreign helper change\n')
                    path.write_text(json.dumps(accepted))
                    with patch.object(ADOPT, 'BASE', base):
                        if defect == 'none': self.assertEqual(accepted, ADOPT.qualification('v2-dr-target'))
                        else:
                            with self.assertRaises(ValueError): ADOPT.qualification('v2-dr-target')
        # Historical target evidence cannot authorize later product changes.
        # The synthetic old/new source fixture above tests the original adoption.
        with patch.object(ADOPT, 'BASE', ROOT):
            if any(ADOPT.digest(ROOT/name) != original['frozen_inputs'][name] for name in dependencies):
                with self.assertRaisesRegex(ValueError, 'target IM adoption prerequisite changed:'):
                    ADOPT.qualification('v2-dr-target')
            else:
                self.assertEqual(original, ADOPT.qualification('v2-dr-target'))
            reconstructed = (ROOT/ADOPT.SOURCE).read_text().replace(ADOPT.NEW_LINES, ADOPT.OLD_LINE)
            if hashlib.sha256(reconstructed.encode()).hexdigest() == original['frozen_inputs'][ADOPT.SOURCE]:
                self.assertEqual(reconstructed, ADOPT.previous_source(original))
            else:
                with self.assertRaisesRegex(ValueError, 'unrelated product changes'):
                    ADOPT.previous_source(original)

    def test_target_managers_use_v2_data_engine_and_exact_single_node(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base/'yml/longhorn/input.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'longhorn_v2_nodes': ['192.168.47.149']}))
            image = 'registry/longhorn-instance-manager:'+ADOPT.NEW_TAG
            original = {'metadata': {'name': 'im', 'uid': 'target-im'}, 'spec': {'nodeID': 'longhorn-node-149',
                'dataEngine': 'v2', 'type': 'aio', 'image': image}, 'status': {'currentState': 'running'}}
            lab = SimpleNamespace(DIRECTORY=base, NODES=('192.168.47.149',), WORKERS=('192.168.47.149',))
            for defect in ('none', 'source-node', 'alias-is-not-dataengine', 'count'):
                rows = [copy.deepcopy(original)]
                if defect == 'source-node': rows[0]['spec']['nodeID'] = 'longhorn-node-146'
                if defect == 'alias-is-not-dataengine': rows[0]['spec']['dataEngine'] = 'v2-dr-target'
                if defect == 'count': rows.append(copy.deepcopy(original))
                lab.get = lambda kind, *a, **kw: {'items': rows} if kind.startswith('instancemanagers') else {'value': image}
                with self.subTest(defect=defect):
                    if defect == 'none': self.assertEqual({'im': {'uid': 'target-im', 'node': 'longhorn-node-149', 'image': image}},
                                                         ADOPT.managers(lab, image, engine='v2-dr-target'))
                    else:
                        with self.assertRaises(ValueError): ADOPT.managers(lab, image, engine='v2-dr-target')

    def test_target_log_accepts_only_original_one_node_business_without_cross_node_claim(self):
        engine = 'v2-dr-target'
        prefix = ADOPT.prefix(engine)
        before = {'config_sha256': 'a'*64, 'required_manager_nodes': ['longhorn-node-149']}
        old = {'old': {'uid': 'old', 'node': 'longhorn-node-149'}}
        runtime = {'new': {'uid': 'new', 'node': 'longhorn-node-149', 'image': 'registry/im:'+ADOPT.NEW_TAG}}
        result = {'phase': 'verified', 'engine': engine, 'before': before, 'old_managers': old, 'new_managers': runtime}
        rows = [prefix+'_PRECONDITIONS_VERIFIED '+json.dumps(before), prefix+'_RUNTIME_VERIFIED '+json.dumps(runtime),
                prefix+'_RESULT '+json.dumps(result), prefix+'_BUSINESS_PASS',
                'LONGHORN_V2_DR_TARGET_INSTALL_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                prefix+'_FIXTURES_CLEAN_PASS', 'LONGHORN_V2_DR_TARGET_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+
                'b'*64+' replicas=1 cross_node=false']
        log = '\n'.join(rows)
        ADOPT.verify_log(log, engine)
        for rejected in (log.replace('cross_node=false', 'cross_node=true'), log.replace('replicas=1', 'replicas=3'),
                         log.replace('longhorn-node-149', 'longhorn-node-146'), log+'\n'+rows[-1],
                         log.replace('LONGHORN_V2_DR_TARGET_INSTALL_BUSINESS_PASS', 'LONGHORN_INSTALL_BUSINESS_PASS'),
                         log+'\nLONGHORN_FIRST_FAILURE phase=target rc=1',
                         log+'\nLONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'b'*64+' replica_nodes=longhorn-node-149'):
            with self.assertRaises(ValueError): ADOPT.verify_log(rejected, engine)

    def test_adoption_reconciles_product_before_business_and_preserves_failure_journal(self):
        for engine, failure in ((e, f) for e in ADOPT.ENGINES for f in ('none', 'setup', 'reuse', 'business')):
            with self.subTest(engine=engine, failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'tests/evidence').mkdir(parents=True)
                (base/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').write_text(
                    json.dumps({'status': 'pass', 'rc': 0, 'failure_markers': 0}))
                (base/'config.yml').write_text('longhorn_version: 1.12.1\nlonghorn_data_engines: ['+('v1' if engine == 'v1' else 'v2')+']\nlonghorn_image_prefix: registry\n')
                phases = []
                def write(path, row):
                    phases.append(row['phase']); path.write_text(json.dumps(row))
                lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'owned'},
                    write_private=write, run_requests=MagicMock(side_effect=ValueError('setup') if failure == 'setup' else None),
                    wait=lambda label, timeout, predicate: predicate(),
                    business=MagicMock(side_effect=ValueError('business') if failure == 'business' else None))
                install = SimpleNamespace(health=MagicMock(), business=MagicMock(
                    side_effect=ValueError('business') if failure == 'business' else None))
                old = {str(i): {'uid': 'old'+str(i)} for i in range(3)}
                new = {str(i): {'uid': ('old' if failure == 'reuse' else 'new')+str(i)} for i in range(3)}
                with patch.object(ADOPT, 'BASE', base), patch.object(ADOPT, 'previous_source', return_value='old'), \
                        patch.object(ADOPT, 'qualification', return_value={'status': 'pass', 'rc': 0, 'failure_markers': 0}), \
                        patch.object(ADOPT, 'baseline', return_value={'stable': True}), \
                        patch.object(ADOPT, 'old_health'), patch.object(ADOPT, 'digest', return_value='sha'), \
                        patch.object(ADOPT, 'managers', side_effect=[old, new, new]):
                    if failure == 'none':
                        ADOPT.adopt(install, lab, 7, engine)
                        self.assertEqual(phases, ['setup-intent', 'business-intent', 'verified'])
                        lab.run_requests.assert_called_once_with(7, [])
                        if engine == 'v2-dr-target':
                            install.business.assert_called_once_with(lab, 7)
                            lab.business.assert_not_called()
                        else:
                            lab.business.assert_called_once_with('install', relay_pid=7)
                    else:
                        with self.assertRaises(ValueError): ADOPT.adopt(install, lab, 7, engine)
                        self.assertNotIn('verified', phases)
                        self.assertTrue((base/ADOPT.RECORD.replace('v2', engine)).exists())
                        if failure in ('setup', 'reuse'): lab.business.assert_not_called()

    def test_log_binds_business_result_and_rejects_missing_failure_duplicate_or_reused_im(self):
        nodes = ['longhorn-node-'+str(n) for n in (146, 147, 148)]
        before = {'config_sha256': 'a'*64, 'required_manager_nodes': sorted(nodes)}
        old = {'old'+str(i): {'uid': 'old'+str(i), 'node': node} for i, node in enumerate(nodes)}
        runtime = {'new'+str(i): {'uid': 'new'+str(i), 'node': node, 'image': 'registry/im:'+ADOPT.NEW_TAG}
                   for i, node in enumerate(nodes)}
        result = {'phase': 'verified', 'engine': 'v2', 'before': before, 'old_managers': old, 'new_managers': runtime}
        def log(row=result):
            return '\n'.join([
                'LONGHORN_V2_IM_ADOPT_PRECONDITIONS_VERIFIED '+json.dumps(before),
                'LONGHORN_V2_IM_ADOPT_RUNTIME_VERIFIED '+json.dumps(runtime),
                'LONGHORN_V2_IM_ADOPT_RESULT '+json.dumps(row),
                'LONGHORN_V2_IM_ADOPT_BUSINESS_PASS', 'LONGHORN_INSTALL_BUSINESS_PASS',
                'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', 'LONGHORN_V2_IM_ADOPT_FIXTURES_CLEAN_PASS',
                'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'b'*64+
                ' replica_nodes=longhorn-node-146,longhorn-node-147,longhorn-node-148'])
        ADOPT.verify_log(log())
        for bad in (log().replace('LONGHORN_INSTALL_BUSINESS_PASS', ''),
                    log()+'\nLONGHORN_FIRST_FAILURE phase=setup rc=1',
                    log()+'\nLONGHORN_INSTALL_BUSINESS_PASS',
                    log(dict(result, new_managers=old))):
            with self.assertRaises(ValueError): ADOPT.verify_log(bad)

    def test_shell_never_uninstalls_or_restores_host_state_and_failure_blocks_pass(self):
        source = (ROOT/'tests/helpers/longhorn-v2-im-adopt.sh').read_text()
        for engine, failure in ((e, f) for e in ADOPT.ENGINES for f in ('none', 'cache', 'business', 'finish', 'verify', 'clean', 'relay-clean')):
            with self.subTest(engine=engine, failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'.venv/bin').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'tests/helpers').mkdir(parents=True)
                (base/'kube-bin').mkdir()
                (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n')
                (base/'kube-bin/kubectl').chmod(0o755)
                helper = '''import os,signal,sys,time
mode=sys.argv[1]; failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as f:f.write(mode+'\\n')
if mode==failure or (failure=='clean' and mode=='business'):sys.exit(17)
'''
                for name in ('core', 'install', 'im-adopt', 'dr-target'):
                    (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                (base/'tests/helpers/longhorn-lab.py').write_text(helper)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-im-adopt.', str(base/'work.'))
                run = subprocess.run(['bash', '-c', script, 'test', engine+'-im-adopt-qualification'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure),
                    text=True, capture_output=True, timeout=10)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertFalse(set(calls) & {'recover', 'destroy', 'restore-hosts', 'configure', 'prepare'})
                self.assertIn('relay-clean', calls)
                if failure == 'none':
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertIn('LONGHORN_'+engine.upper().replace('-', '_')+'_IM_ADOPT_QUALIFICATION_PASS', run.stdout)
                    self.assertLess(calls.index('cache'), calls.index('business'))
                else:
                    self.assertNotEqual(run.returncode, 0)
                    self.assertNotIn('LONGHORN_'+engine.upper().replace('-', '_')+'_IM_ADOPT_QUALIFICATION_PASS', run.stdout)
                    if failure != 'relay-clean': self.assertIn('clean', calls)


if __name__ == '__main__':
    unittest.main()
