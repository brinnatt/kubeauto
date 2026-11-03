"""Exact Manager pin eligibility and real runtime/adoption refusal contracts."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('manager_adopt_contract', ROOT/'tests/helpers/longhorn-manager-adopt.py')
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


class ManagerAdoptionContracts(unittest.TestCase):
    def pending_pins(self, base):
        prior, publication, write = self.pins(base)
        inputs = {}
        for name in prior['inputs']:
            path = base/name
            old = path.read_text()
            new = old.replace(MODULE.NEW, MODULE.PENDING_NEW)
            inputs[name] = {'old_sha256': hashlib.sha256(old.encode()).hexdigest(),
                            'sha256': write(name, new)}
        publication = copy.deepcopy(publication)
        publication['image'] = publication['publication_record']['key'] = 'brinnatt/longhorn-manager:'+MODULE.PENDING_NEW
        path = 'tests/evidence/longhorn-manager-pending-publication-20261102.json'
        row = {'scope': 'manager-v2-pending-pin-only', 'old_tag': MODULE.NEW,
               'new_tag': MODULE.PENDING_NEW, 'inputs': inputs,
               'publication': {'path': path, 'sha256': write(path, publication)}}
        write(MODULE.PENDING_PLAN, row)
        return prior, row, publication, write

    def test_pending_transition_preserves_both_exact_historical_pin_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); prior, row, _, _ = self.pending_pins(base)
            with patch.object(MODULE, 'BASE', base):
                for name, edge in row['inputs'].items():
                    self.assertTrue(MODULE.input_current(name, edge['old_sha256']))
                    self.assertTrue(MODULE.input_current(name, prior['inputs'][name]['old_sha256']))
                    self.assertFalse(MODULE.input_current(name, '0'*64))
                    saved = (base/name).read_bytes()
                    (base/name).write_bytes(saved+b'unrelated change\n')
                    self.assertFalse(MODULE.input_current(name, edge['old_sha256']))
                    (base/name).write_bytes(saved)

    def test_pending_transition_refuses_wrong_plan_or_unpublished_artifact(self):
        for defect in ('scope', 'old', 'new', 'publication-path', 'publication-sha', 'failed', 'component', 'architecture'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); _, row, publication, write = self.pending_pins(base)
                if defect == 'scope': row['scope'] = 'unrelated'
                if defect == 'old': row['old_tag'] = MODULE.OLD
                if defect == 'new': row['new_tag'] = 'v1.12.1-kubeauto.3'
                if defect == 'publication-path': row['publication']['path'] = MODULE.PLAN
                if defect == 'publication-sha': row['publication']['sha256'] = '0'*64
                if defect == 'failed': publication['durable_rc'] = 1
                if defect == 'component': publication['image'] = 'brinnatt/longhorn-instance-manager:'+MODULE.PENDING_NEW
                if defect == 'architecture': publication['publication_record']['platforms'].pop('linux/arm64')
                if defect in ('failed', 'component', 'architecture'):
                    row['publication']['sha256'] = write(row['publication']['path'], publication)
                write(MODULE.PENDING_PLAN, row)
                with patch.object(MODULE, 'BASE', base), self.assertRaises(ValueError): MODULE.plan(pending=True)

    def test_pending_rollout_uses_product_once_and_preserves_old_adoption_contract(self):
        for already_current in (False, True):
            with self.subTest(already_current=already_current), tempfile.TemporaryDirectory() as directory:
                lab, ds, pods, record = self.runtime_fixture(Path(directory))
                before = {'identity': 'baseline'}
                baseline = SimpleNamespace(capture=Mock(return_value=before), stable_installation=Mock())
                def rollout(pid, requests):
                    self.assertEqual((17, []), (pid, requests))
                    ds['spec']['template']['spec']['containers'][0]['image'] = 'registry/longhorn-manager:'+MODULE.PENDING_NEW
                    for pod in pods:
                        pod['metadata']['uid'] += '-new'
                        pod['spec']['containers'][0]['image'] = ds['spec']['template']['spec']['containers'][0]['image']
                if already_current: rollout(17, [])
                lab.run_requests.side_effect = rollout
                with patch.object(MODULE, 'plan', return_value=({}, record)) as plan, patch.object(MODULE, 'load', return_value=baseline), redirect_stdout(io.StringIO()):
                    MODULE.adopt(lab, 17, pending=True)
                plan.assert_called_once_with(pending=True)
                if already_current:
                    lab.run_requests.assert_not_called(); lab.cache_artifacts.assert_not_called()
                else:
                    lab.run_requests.assert_called_once_with(17, [])
                    lab.cache_artifacts.assert_called_once_with({'brinnatt/longhorn-manager:'+MODULE.PENDING_NEW})
                baseline.stable_installation.assert_called_once_with(before, before)

    def test_integrity_handoff_adopts_before_baseline_and_native_restore(self):
        shell = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        adoption = shell.index('"$BASE/tests/helpers/longhorn-manager-adopt.py" --pending')
        self.assertLess(shell.index('"$PY" "$CORE" verify-transport'), adoption)
        self.assertLess(adoption, shell.index('"$PY" "$INSTALL" volume-baseline'))
        self.assertLess(adoption, shell.index('longhorn-v2-integrity.py" business'))
        self.assertIn('if [[ "$mode" == v2-integrity-qualification ]]; then\n  "$PY" "$BASE/tests/helpers/longhorn-manager-adopt.py" --pending\nfi', shell)

    def pins(self, base):
        def write(name, data):
            path = base/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data) if isinstance(data, dict) else data)
            return hashlib.sha256(path.read_bytes()).hexdigest()
        original = {MODULE.CONSTANTS: 'if target:\n', MODULE.CATALOG: '{"longhorn-manager": "v1.12.1"}'}
        current = {MODULE.CONSTANTS: original[MODULE.CONSTANTS]+'            tags["longhorn-manager"] = "'+MODULE.NEW+'"\n',
                   MODULE.CATALOG: original[MODULE.CATALOG].replace(MODULE.OLD, MODULE.NEW)}
        inputs = {name: {'old_sha256': hashlib.sha256(original[name].encode()).hexdigest(),
                         'sha256': write(name, data)} for name, data in current.items()}
        publication = {'status': 'pass', 'durable_rc': 0, 'failure_markers': 0, 'selected_image_count': 1,
            'image': 'brinnatt/longhorn-manager:'+MODULE.NEW, 'publication_record': {
                'key': 'brinnatt/longhorn-manager:'+MODULE.NEW, 'digest': 'sha256:'+'a'*64,
                'platforms': {'linux/amd64': 'sha256:'+'b'*64, 'linux/arm64': 'sha256:'+'c'*64}}}
        path = 'tests/evidence/longhorn-manager-eviction-publication-20261024.json'
        row = {'scope': 'manager-linked-eviction-pin-only', 'old_tag': MODULE.OLD, 'new_tag': MODULE.NEW,
               'inputs': inputs, 'publication': {'path': path, 'sha256': write(path, publication)}}
        write(MODULE.PLAN, row)
        return row, publication, write

    def test_only_exact_reverse_hashes_grant_historical_eligibility(self):
        for defect in ('none', 'other-source', 'unknown-origin', 'other-pin', 'duplicate'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); row, _, _ = self.pins(base)
                for name in row['inputs']:
                    path = base/name; original = row['inputs'][name]['old_sha256']
                    saved = path.read_bytes()
                    if defect == 'other-source': path.write_bytes(saved+b'other change\n')
                    if defect == 'other-pin': path.write_text(path.read_text().replace(MODULE.NEW, 'v1.12.1-kubeauto.9'))
                    if defect == 'duplicate': path.write_bytes(saved+saved)
                    if defect == 'unknown-origin': original = '0'*64
                    with patch.object(MODULE, 'BASE', base):
                        self.assertEqual(defect == 'none', MODULE.input_current(name, original))
                    path.write_bytes(saved)

    def test_unpublished_failed_or_changed_publication_cannot_authorize_reuse(self):
        for defect in ('pending', 'durable', 'failure', 'count', 'component', 'arch', 'sha'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); row, publication, write = self.pins(base)
                if defect == 'pending': publication['status'] = 'pending'
                if defect == 'durable': publication['durable_rc'] = 1
                if defect == 'failure': publication['failure_markers'] = 1
                if defect == 'count': publication['selected_image_count'] = 2
                if defect == 'component': publication['image'] = 'brinnatt/longhorn-instance-manager:'+MODULE.NEW
                if defect == 'arch': publication['publication_record']['platforms'].pop('linux/arm64')
                if defect == 'sha': publication['changed'] = True
                value = write(row['publication']['path'], publication)
                if defect != 'sha': row['publication']['sha256'] = value
                write(MODULE.PLAN, row)
                with patch.object(MODULE, 'BASE', base), self.assertRaises(ValueError): MODULE.plan()

    def runtime_fixture(self, base, *, old=False):
        directory = base/'cluster'; (directory/'yml/longhorn').mkdir(parents=True)
        (directory/'yml/longhorn/input.json').write_text('{"longhorn_image_prefix":"registry"}')
        ds = {'metadata': {'uid': 'ds'}, 'spec': {'template': {'spec': {'containers': [
            {'name': 'longhorn-manager', 'image': 'registry/longhorn-manager:'+(MODULE.OLD if old else MODULE.NEW)}]}}}}
        pods = [{'metadata': {'uid': 'pod'+str(n), 'ownerReferences': [
                    {'kind': 'DaemonSet', 'uid': 'ds', 'controller': True}]},
                 'spec': {'nodeName': 'longhorn-node-'+str(n), 'containers': copy.deepcopy(ds['spec']['template']['spec']['containers'])},
                 'status': {'containerStatuses': [{'name': 'longhorn-manager', 'ready': True,
                    'state': {'running': {'startedAt': '2026-10-24T00:00:00Z'}}, 'imageID': 'registry/longhorn-manager@sha256:'+'b'*64}]}}
                for n in (146, 147, 148)]
        lab = SimpleNamespace(DIRECTORY=directory, WORKERS=('192.168.47.146', '192.168.47.147', '192.168.47.148'),
            get=Mock(side_effect=lambda kind, *a, **kw: ds if kind == 'daemonset' else {'items': pods}),
            cache_artifacts=Mock(), run_requests=Mock(), installation_health=Mock(),
            publication_records=Mock(return_value=([], [])))
        record = {'digest': 'sha256:'+'a'*64, 'platforms': {'linux/amd64': 'sha256:'+'b'*64, 'linux/arm64': 'sha256:'+'c'*64}}
        return lab, ds, pods, record

    def test_tag_alone_wrong_owner_missing_duplicate_unready_and_wrong_digest_refused(self):
        for defect in ('none', 'digest', 'owner', 'missing', 'duplicate', 'unready', 'deleting', 'tag'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, _, pods, record = self.runtime_fixture(Path(directory))
                if defect == 'digest': pods[0]['status']['containerStatuses'][0]['imageID'] = 'containerd://sha256:'+'d'*64
                if defect == 'owner': pods[0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if defect == 'missing': pods.pop()
                if defect == 'duplicate': pods.append(copy.deepcopy(pods[0]))
                if defect == 'unready': pods[0]['status']['containerStatuses'][0]['ready'] = False
                if defect == 'deleting': pods[0]['metadata']['deletionTimestamp'] = 'now'
                if defect == 'tag': pods[0]['spec']['containers'][0]['image'] += '-wrong'
                if defect == 'none': self.assertEqual(3, len(MODULE.runtime(lab, record)['pods']))
                else:
                    with self.assertRaises(ValueError): MODULE.runtime(lab, record)

    def test_running_digest_can_bind_only_exact_published_config_and_ordered_layers(self):
        spec = importlib.util.spec_from_file_location('manager_manifest_contract', ROOT/'tests/helpers/longhorn-lab.py')
        helpers = importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
        for defect in ('none', 'config', 'layer', 'order', 'bytes', 'schema', 'catalog'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, _, pods, record = self.runtime_fixture(Path(directory))
                expected = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                    'config': {'digest': 'sha256:'+'d'*64, 'size': 20},
                    'layers': [{'digest': 'sha256:'+'e'*64, 'size': 30},
                               {'digest': 'sha256:'+'f'*64, 'size': 40}]}
                actual = copy.deepcopy(expected)
                actual['mediaType'] = 'application/vnd.docker.distribution.manifest.v2+json'
                if defect == 'config': actual['config']['digest'] = 'sha256:'+'0'*64
                if defect == 'layer': actual['layers'][0]['size'] += 1
                if defect == 'order': actual['layers'].reverse()
                if defect == 'schema': actual['schemaVersion'] = 1
                raw = json.dumps(actual)
                runtime_digest = 'sha256:'+hashlib.sha256(raw.encode()).hexdigest()
                if defect == 'bytes': runtime_digest = 'sha256:'+'0'*64
                for pod in pods:
                    pod['status']['containerStatuses'][0]['imageID'] = 'registry/longhorn-manager@'+runtime_digest
                row = dict(record, key='brinnatt/longhorn-manager:'+MODULE.NEW)
                if defect == 'catalog': row['platforms'] = {'linux/amd64': 'sha256:'+'0'*64}
                lab.publication_records.return_value = ([row], [])
                lab.published_manifest = Mock(return_value=expected)
                lab.equivalent_manifest = helpers.equivalent_manifest
                lab.execute = Mock(return_value=SimpleNamespace(stdout=raw))
                if defect == 'none':
                    self.assertEqual(3, len(MODULE.runtime(lab, record)['pods']))
                    lab.execute.assert_called_once()
                    self.assertEqual(lab.execute.call_args.args[0][-1],
                        'docker://127.0.0.1:5000/brinnatt/longhorn-manager@'+runtime_digest)
                else:
                    with self.assertRaisesRegex(ValueError, 'running digest'): MODULE.runtime(lab, record)

    def test_rollout_uses_product_and_verifies_new_pods_then_unchanged_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, ds, pods, record = self.runtime_fixture(Path(directory), old=True)
            before = {'identity': 'baseline'}
            baseline = SimpleNamespace(capture=Mock(return_value=before), stable_installation=Mock())
            def rollout(pid, requests):
                self.assertEqual(17, pid); self.assertEqual([], requests)
                ds['spec']['template']['spec']['containers'][0]['image'] = 'registry/longhorn-manager:'+MODULE.NEW
                for pod in pods:
                    pod['metadata']['uid'] += '-new'
                    pod['spec']['containers'][0]['image'] = ds['spec']['template']['spec']['containers'][0]['image']
            lab.run_requests.side_effect = rollout
            with patch.object(MODULE, 'plan', return_value=({}, record)), patch.object(MODULE, 'load', return_value=baseline), redirect_stdout(io.StringIO()) as out:
                MODULE.adopt(lab, 17)
            lab.run_requests.assert_called_once_with(17, [])
            lab.cache_artifacts.assert_called_once_with({'brinnatt/longhorn-manager:'+MODULE.NEW})
            baseline.stable_installation.assert_called_once_with(before, before)
            self.assertIn('LONGHORN_MANAGER_ADOPT_RESULT ', out.getvalue())
            self.assertNotIn('LONGHORN_MANAGER_ADOPT_PASS', out.getvalue())

    def test_fresh_current_runtime_is_verified_without_repeated_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, _, _, record = self.runtime_fixture(Path(directory))
            baseline = SimpleNamespace(capture=Mock(return_value={}), stable_installation=Mock())
            with patch.object(MODULE, 'plan', return_value=({}, record)), patch.object(MODULE, 'load', return_value=baseline), redirect_stdout(io.StringIO()): MODULE.adopt(lab, 17)
            lab.run_requests.assert_not_called(); lab.cache_artifacts.assert_not_called()

    def test_shell_adopts_before_snapshot_and_business_while_python_owns_its_relay(self):
        shell = (ROOT/'tests/helpers/longhorn-v2-linked-acceptance.sh').read_text()
        self.assertLess(shell.index('longhorn-manager-adopt.py'), shell.index('snapshot --phase before'))
        self.assertLess(shell.index('snapshot --phase before'), shell.index('for mode in linked-clone'))
        for fail in (False, True):
            with self.subTest(fail=fail):
                lab = SimpleNamespace(require_lease=Mock(), relay_ensure=Mock(), relay_cleanup=Mock(), core_identity=Mock())
                install = SimpleNamespace(fixture=Mock(return_value=(None, lab)))
                core = SimpleNamespace(verify_transport=Mock())
                process = SimpleNamespace(pid=42, terminate=Mock(), wait=Mock(), returncode=0)
                with patch.object(MODULE, 'load', side_effect=[install, core]), patch.object(MODULE.subprocess, 'Popen', return_value=process), patch.object(MODULE, 'adopt', side_effect=ValueError('first failure') if fail else None) as adopt, redirect_stdout(io.StringIO()) as output:
                    if fail:
                        with self.assertRaisesRegex(ValueError, 'first failure'): MODULE.main()
                    else: MODULE.main()
                core.verify_transport.assert_called_once_with(lab)
                lab.require_lease.assert_called_once(); lab.relay_ensure.assert_called_once()
                adopt.assert_called_once_with(lab, 42)
                process.terminate.assert_called_once(); process.wait.assert_called_once_with(timeout=30)
                lab.relay_cleanup.assert_called_once(); lab.core_identity.assert_called_once()
                self.assertEqual(not fail, 'LONGHORN_MANAGER_ADOPT_PASS' in output.getvalue())

    def test_failed_relay_exit_or_cleanup_cannot_emit_adoption_success(self):
        for defect in ('relay', 'cleanup', 'launch'):
            with self.subTest(defect=defect):
                lab = SimpleNamespace(require_lease=Mock(), relay_ensure=Mock(), relay_cleanup=Mock(), core_identity=Mock())
                install = SimpleNamespace(fixture=Mock(return_value=(None, lab)))
                core = SimpleNamespace(verify_transport=Mock())
                process = SimpleNamespace(pid=42, terminate=Mock(), wait=Mock(), returncode=1 if defect == 'relay' else 0)
                if defect == 'cleanup': lab.relay_cleanup.side_effect = ValueError('cleanup failed')
                with patch.object(MODULE, 'load', side_effect=[install, core]), patch.object(MODULE.subprocess, 'Popen', side_effect=OSError('launch failed') if defect == 'launch' else None, return_value=process), patch.object(MODULE, 'adopt'), redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises((ValueError, OSError)): MODULE.main()
                lab.relay_cleanup.assert_called_once()
                self.assertNotIn('LONGHORN_MANAGER_ADOPT_PASS', output.getvalue())

    def test_real_relay_child_keeps_fixed_leases_and_closes_unrelated_descriptor(self):
        # Isolate descriptor numbers from the unit runner. The child executes
        # the actual lease guard against private lock files, without a lab.
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); helpers=base/'tests/helpers';helpers.mkdir(parents=True)
            (base/'.venv').symlink_to(ROOT/'.venv',target_is_directory=True)
            bootstrap=textwrap.dedent('''\
                import importlib.util,os,sys
                from pathlib import Path
                root=Path(sys.argv[1]);base=Path(sys.argv[2])
                def load(name):
                    spec=importlib.util.spec_from_file_location(name,root/'tests/helpers'/name)
                    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
                lab=load('longhorn-lab.py')
                lab.Path=lambda value: base if value=='/var/lock' else Path(value)
                ''')
            child=bootstrap+textwrap.dedent('''\
                import json,signal
                lab.require_lease()
                try:os.fstat(40)
                except OSError:pass
                else:raise RuntimeError('unrelated descriptor leaked')
                signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
                (base/'relay-ready').write_text('leases-verified')
                while True:signal.pause()
                ''')
            # Match the real helper's relay-maintain argv; fixture arguments
            # are embedded in the isolated script, never production inputs.
            child=child.replace('root=Path(sys.argv[1]);base=Path(sys.argv[2])',
                'root=Path('+repr(str(ROOT))+');base=Path('+repr(str(base))+')')
            (helpers/'longhorn-v2-install.py').write_text(child)
            parent=bootstrap+textwrap.dedent('''\
                import time
                from types import SimpleNamespace
                from unittest.mock import Mock
                files=[]
                for descriptor,name in ((7,'kubeauto-ceph-compute.lock'),(8,'kubeauto-redis-core-pool.lock'),(9,'kubeauto-ceph-priority.lock')):
                    f=open(base/name,'w');files.append(f)
                    os.dup2(f.fileno(),descriptor,inheritable=True)
                os.dup2(files[0].fileno(),40,inheritable=True)
                manager=load('longhorn-manager-adopt.py');manager.BASE=base
                fixture=SimpleNamespace(require_lease=lab.require_lease,relay_ensure=Mock(),relay_cleanup=Mock(),core_identity=Mock())
                manager.load=lambda name: SimpleNamespace(fixture=lambda:(None,fixture)) if name=='longhorn-v2-install.py' else SimpleNamespace(verify_transport=lambda _:None)
                def adopt(fixture,pid):
                    deadline=time.monotonic()+3
                    while not (base/'relay-ready').exists():
                        if time.monotonic()>deadline:raise RuntimeError('relay child did not retain its fixed leases')
                        time.sleep(.02)
                manager.adopt=adopt
                manager.main()
                fixture.relay_cleanup.assert_called_once();fixture.core_identity.assert_called_once()
                ''')
            result=subprocess.run([sys.executable,'-c',parent,str(ROOT),str(base)],
                capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('LONGHORN_MANAGER_ADOPT_PASS',result.stdout)


if __name__ == '__main__': unittest.main()
