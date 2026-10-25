"""Single-key stale fixture repair must fail before a conflicting cache write."""
import copy
import base64
import os
import sys
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('reconcile_lab', ROOT/'tests/helpers/longhorn-lab.py')
LAB = importlib.util.module_from_spec(spec); spec.loader.exec_module(LAB)


def digest(raw):
    return 'sha256:'+hashlib.sha256(raw.encode()).hexdigest()


class CacheReconcileContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.parent = tempfile.TemporaryDirectory(prefix='kubeauto-longhorn-parallel.', dir='/var/tmp')
        self.addCleanup(self.parent.cleanup)
        (Path(self.parent.name)/'first-failure.status').write_text('rc=1\n')
        self.revision = '5'*40
        config = {'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': {
            'org.opencontainers.image.source': 'https://github.com/brinnatt/kubeauto-ext-images-dockerfile',
            'org.opencontainers.image.revision': self.revision}}}
        config_raw = json.dumps(config)
        self.old_raw = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'config': {'digest': digest(config_raw), 'size': len(config_raw.encode())},
            'layers': [{'digest': 'sha256:'+'a'*64, 'size': 1}]})
        self.new_raw = self.old_raw.replace('a'*64, 'b'*64)
        self.current_raw = self.old_raw
        self.row = {'key': 'brinnatt/busybox:1.37', 'platforms': {'linux/amd64': digest(self.new_raw)},
            'talkedu_ref': 'hub.talkedu.cn/kubeauto/busybox:1.37', 'dockerhub_ref': 'brinnatt/busybox:1.37'}
        self.proof = {'key': self.row['key'], 'run_id': 'run', 'expected_child': digest(self.new_raw),
            'previous_manifest': self.old_raw, 'previous_digest': digest(self.old_raw), 'previous_config': config_raw,
            'historical_build': {'revision': self.revision,
                'path': 'platform/observability/prometheus/busybox/Dockerfile',
                'dockerfile': '# brinnatt/busybox\nFROM busybox:1.37\n',
                'dockerfile_sha256': hashlib.sha256(b'# brinnatt/busybox\nFROM busybox:1.37\n').hexdigest()},
            'product_sources': {}, 'registry': {'id': 'container', 'created': 'created', 'mount_source': '/data/registry'},
            'completed_parent': {'directory': self.parent.name, 'pid': 98765432, 'rc': 1},
            'consumer_clusters': {}}
        self.proof['completed_parent']['branches'] = {}
        for branch, pid, rc in [('v1',98765433,0),('v2',98765434,1)]:
            directory=Path(self.parent.name)
            log='LONGHORN_PARALLEL_'+branch.upper()+'_EXIT rc='+str(rc)+'\n'
            if branch=='v1': log=log+'nested accepted stage\n'+log
            for suffix,raw in [('.pid',str(pid)+'\n'),('.exit',str(rc)+'\n'),
                               ('.finalized',str(rc)+'\n'),('.log',log)]:
                (directory/(branch+suffix)).write_text(raw)
            self.proof['completed_parent']['branches'][branch] = {
                'pid':pid,'rc':rc,'log_sha256':hashlib.sha256(log.encode()).hexdigest()}
        for relative in ('service/cluster/registry.py', 'common/constants.py'):
            path = self.root/relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(relative)
            self.proof['product_sources'][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        for cluster in ('longhorn-gate', 'longhorn-v2-gate', 'longhorn-v2-dr-target'):
            root = self.root/'clusters'/cluster; root.mkdir(parents=True)
            (root/'longhorn-lab-owner.json').write_text(json.dumps({'run_id': cluster, 'cluster_uid': cluster}))
            self.proof['consumer_clusters'][cluster] = {'run_id': cluster}
        self.owned = self.root/'clusters/longhorn-v2-gate'
        self.registry = {'id': 'container', 'created': 'created', 'running': True, 'image': 'brinnatt/registry:2',
            'mounts': [{'Destination': '/var/lib/registry', 'Source': '/data/registry', 'Type': 'bind', 'RW': True}],
            'ports': {'5000/tcp': [{'HostIp': '', 'HostPort': '5000'}]}}
        self.workloads = {'items': []}
        self.docker_images = 'brinnatt/registry:2\n'
        self.failures = 0; self.copy_rc = 0; self.copy_corrupt = False; self.old_available = True
        self.calls = []
        def execute(argv, **kwargs):
            self.calls.append(argv)
            if argv[0] == 'docker':
                raw = json.dumps(self.registry) if argv[1] == 'inspect' else self.docker_images
            elif argv[0] == 'kubectl':
                cluster = Path(argv[1].split('=', 1)[1]).parent.name
                raw = json.dumps({'metadata': {'uid': cluster}} if 'kube-system' in argv else self.workloads)
            elif argv[:2] == ['skopeo', 'inspect']:
                if '@' in argv[-1]:
                    if not self.old_available: raise ValueError('manifest unavailable')
                    raw = self.old_raw
                else: raw = self.current_raw
            elif argv[:2] == ['skopeo', 'copy']:
                if self.failures:
                    self.failures -= 1
                    return subprocess.CompletedProcess(argv, 1, '', 'fixed source unavailable')
                if self.copy_corrupt:
                    self.current_raw = '{}'
                elif self.copy_rc == 0:
                    self.current_raw = self.old_raw if '--src-tls-verify=false' in argv else self.new_raw
                return subprocess.CompletedProcess(argv, self.copy_rc, '', 'copy result')
            else: self.fail('unexpected command '+repr(argv))
            return subprocess.CompletedProcess(argv, 0, raw, '')
        for name, value in [('BASE', self.root), ('DIRECTORY', self.owned), ('owner', Mock(return_value={'run_id': 'run'})),
            ('require_lease', Mock()), ('process_identity', Mock(return_value=None)), ('execute', Mock(side_effect=execute)),
            ('published_manifest', Mock(return_value=json.loads(self.new_raw)))]:
            context = patch.object(LAB, name, value); context.start(); self.addCleanup(context.stop)

    def run_repair(self, **kwargs):
        LAB.reconcile_cache_image(self.row, self.proof, **kwargs)

    def copies(self):
        return [a for a in self.calls if a[:2] == ['skopeo', 'copy']]

    def test_monitoring_reconciles_only_one_key_then_reuses_existing_cache_path(self):
        spec = importlib.util.spec_from_file_location('reconcile_monitoring', ROOT/'tests/helpers/longhorn-v2-monitoring.py')
        monitoring = importlib.util.module_from_spec(spec); spec.loader.exec_module(monitoring)
        from types import SimpleNamespace
        evidence = json.loads((ROOT/'tests/evidence/longhorn-prometheus-images-20261021.json').read_text())
        original = Mock(return_value=([], []))
        staged = []
        def cache(**kwargs):
            production, support = lab.publication_records()
            self.assertEqual(len(production), 11); self.assertEqual(support, [])
            staged.append(kwargs)
        lab = SimpleNamespace(BASE=ROOT, publication_records=original, cache_artifacts=cache)
        monitoring.cache(lab, reconcile=self.proof)
        self.assertEqual(staged[0], {'keys': {'brinnatt/busybox:1.37'}, 'reconcile': self.proof})
        self.assertEqual(staged[1], {'keys': {r['key'] for r in evidence['images']}})
        self.assertIs(lab.publication_records, original)

    def test_actual_parallel_parent_has_no_exit_and_uses_both_real_branch_states(self):
        self.assertFalse((Path(self.parent.name)/'exit').exists())
        self.run_repair()
        self.assertEqual(len(self.copies()),1)
        self.assertFalse((Path(self.parent.name)/'exit').exists())

    def test_either_child_active_missing_fence_pid_or_rc_drift_blocks_copy(self):
        parent=Path(self.parent.name)
        for branch in ('v1','v2'):
            child=self.proof['completed_parent']['branches'][branch]
            for fault in ('active','missing-finalized','pid-drift','exit-drift','finalized-drift'):
                with self.subTest(branch=branch,fault=fault):
                    suffix={'missing-finalized':'.finalized','pid-drift':'.pid',
                        'exit-drift':'.exit','finalized-drift':'.finalized'}.get(fault)
                    path=parent/(branch+suffix) if suffix else None
                    old=path.read_text() if path else None
                    if fault=='active': LAB.process_identity.side_effect=lambda pid: ('live',) if pid==child['pid'] else None
                    elif fault=='missing-finalized': path.unlink()
                    else: path.write_text('99\n')
                    with self.assertRaisesRegex(ValueError,'branch'): self.run_repair()
                    if path:path.write_text(old)
                    LAB.process_identity.side_effect=None
        self.assertEqual(self.copies(),[])

    def test_branch_log_hash_terminal_marker_or_missing_branch_rejected(self):
        for fault in ('hash','marker','missing-branch'):
            proof=copy.deepcopy(self.proof)
            parent=Path(self.parent.name)
            original=(parent/'v2.log').read_bytes()
            if fault=='hash':proof['completed_parent']['branches']['v2']['log_sha256']='0'*64
            elif fault=='marker':
                raw=b'no terminal marker\n';(parent/'v2.log').write_bytes(raw)
                proof['completed_parent']['branches']['v2']['log_sha256']=hashlib.sha256(raw).hexdigest()
            else:del proof['completed_parent']['branches']['v1']
            with self.subTest(fault=fault),self.assertRaisesRegex(ValueError,'branch'):
                LAB.reconcile_cache_image(self.row,proof)
            (parent/'v2.log').write_bytes(original)
        self.assertEqual(self.copies(),[])

    def test_exact_forward_copy_and_durable_old_raw_rollback(self):
        self.run_repair()
        calls = self.copies(); self.assertEqual(len(calls), 1)
        self.assertIn('--preserve-digests', calls[0])
        self.assertEqual(calls[0][-2], 'docker://hub.talkedu.cn/kubeauto/busybox@'+digest(self.new_raw))
        self.assertEqual(calls[0][-1], 'docker://127.0.0.1:5000/brinnatt/busybox:1.37')
        receipt = json.loads((self.owned/'longhorn-busybox-cache-reconcile.json').read_text())
        self.assertEqual(receipt['phase'], 'verified')
        self.assertEqual(receipt['proof']['previous_manifest'], self.old_raw)
        self.assertEqual(receipt['proof']['previous_config'], self.proof['previous_config'])
        self.run_repair(restore=True)
        self.assertEqual(self.current_raw, self.old_raw)
        self.assertEqual(len(self.copies()), 2)
        self.assertIn('--src-tls-verify=false', self.copies()[1])
        self.assertEqual(self.copies()[1][-2], 'docker://127.0.0.1:5000/brinnatt/busybox@'+digest(self.old_raw))

    def test_missing_owner_and_changed_publication_have_no_mutation(self):
        for field, value in [('run_id', 'foreign'), ('expected_child', 'sha256:'+'c'*64), ('key', 'other:1')]:
            with self.subTest(field=field):
                proof = copy.deepcopy(self.proof); proof[field] = value
                with self.assertRaises(ValueError): LAB.reconcile_cache_image(self.row, proof)
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError): LAB.reconcile_cache_image(self.row, None)

    def test_missing_lease_rejects_before_all_commands(self):
        LAB.require_lease.side_effect = ValueError('lease absent')
        with self.assertRaisesRegex(ValueError, 'lease absent'): self.run_repair()
        self.assertEqual(self.calls, [])

    def test_active_parent_blocks_before_all_commands(self):
        LAB.process_identity.return_value = ('live',)
        with self.assertRaisesRegex(ValueError, 'parent'): self.run_repair()
        self.assertEqual(self.calls, [])

    def test_parent_durable_state_change_refused(self):
        (Path(self.parent.name)/'first-failure.status').write_text('rc=0\n')
        with self.assertRaisesRegex(ValueError, 'terminal'): self.run_repair()
        self.assertEqual(self.copies(), [])

    def test_product_source_or_historical_build_conflict_refused(self):
        for fault in ('source', 'build', 'config'):
            with self.subTest(fault=fault):
                proof = copy.deepcopy(self.proof)
                if fault == 'source': proof['product_sources']['common/constants.py'] = '0'*64
                if fault == 'build': proof['historical_build']['revision'] = '6'*40
                if fault == 'config': proof['previous_config'] = '{}'
                with self.assertRaises(ValueError): LAB.reconcile_cache_image(self.row, proof)
        self.assertEqual(self.copies(), [])

    def test_registry_replacement_or_foreign_mount_refused(self):
        for field, value in [('id', 'foreign'), ('mounts', [{'Destination': '/var/lib/registry', 'Type': 'bind',
                'Source': '/foreign', 'RW': True}]), ('ports', {'5000/tcp': []})]:
            with self.subTest(field=field):
                original = self.registry; self.registry = dict(original, **{field: value})
                with self.assertRaisesRegex(ValueError, 'RegistryManager'): self.run_repair()
                self.registry = original
        self.assertEqual(self.copies(), [])

    def test_tag_identity_drift_is_compare_before_copy_failure(self):
        self.current_raw = '{}'
        with self.assertRaisesRegex(ValueError, 'compare-before-copy'): self.run_repair()
        self.assertEqual(self.copies(), [])

    def test_old_raw_unavailable_prevents_copy(self):
        self.old_available = False
        with self.assertRaisesRegex(ValueError, 'unavailable'): self.run_repair()
        self.assertEqual(self.copies(), [])

    def test_all_shared_workload_consumers_are_checked(self):
        self.workloads = {'items': [{'spec': {'jobTemplate': {'spec': {'template': {'spec': {
            'initContainers': [{'name': 'fixture', 'image': 'registry.talkschool.cn:5000/brinnatt/busybox:1.37'}]}}}}}}]}
        with self.assertRaisesRegex(ValueError, 'consumer conflict'): self.run_repair()
        self.assertEqual(self.copies(), [])

    def test_docker_busybox_consumer_blocks_copy(self):
        self.docker_images += 'brinnatt/busybox:1.37\n'
        with self.assertRaisesRegex(ValueError, 'Docker consumer'): self.run_repair()
        self.assertEqual(self.copies(), [])

    def test_incomplete_consumer_boundary_refused(self):
        del self.proof['consumer_clusters']['longhorn-gate']
        with self.assertRaisesRegex(ValueError, 'boundary'): self.run_repair()
        self.assertEqual(self.copies(), [])

    def test_failed_first_source_falls_back_only_at_immutable_child(self):
        self.failures = 1
        self.run_repair()
        self.assertEqual(len(self.copies()), 2)
        self.assertEqual(self.copies()[1][-2], 'docker://docker.io/brinnatt/busybox@'+digest(self.new_raw))
        self.assertTrue(all(a[-1] == 'docker://127.0.0.1:5000/brinnatt/busybox:1.37' for a in self.copies()))

    def test_failed_copy_preserves_old_tag_and_real_receipt_failure(self):
        self.copy_rc = 1
        with self.assertRaisesRegex(ValueError, 'old identity retained'): self.run_repair()
        self.assertEqual(self.current_raw, self.old_raw)
        receipt = json.loads((self.owned/'longhorn-busybox-cache-reconcile.json').read_text())
        self.assertEqual(receipt['copy_rc'], 1); self.assertEqual(receipt['phase'], 'copy-returned')

    def test_copy_side_effect_drift_stops_without_overwriting_unknown_tag(self):
        self.copy_corrupt = True
        with self.assertRaisesRegex(ValueError, 'explicit recovery'): self.run_repair()
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.current_raw, '{}')

    def test_bulk_keys_cannot_enable_reconcile(self):
        with patch.object(LAB, 'publication_records', return_value=([self.row, dict(self.row, key='brinnatt/other:1')], [])):
            with self.assertRaisesRegex(ValueError, 'single BusyBox'): LAB.cache_artifacts(
                keys={self.row['key'], 'brinnatt/other:1'}, reconcile=self.proof)
        self.assertEqual(self.copies(), [])

    def test_failed_copy_retry_reuses_same_receipt_and_retains_first_failure(self):
        self.copy_rc = 1
        with self.assertRaises(ValueError): self.run_repair()
        self.copy_rc = 0
        self.run_repair()
        receipt = json.loads((self.owned/'longhorn-busybox-cache-reconcile.json').read_text())
        self.assertEqual(receipt['phase'], 'verified')
        self.assertEqual([x['rc'] for x in receipt['attempts']], [1, 1, 0])
        self.assertEqual(receipt['attempts'][0]['stderr'], 'copy result')

    def test_unknown_copy_result_can_restore_exact_old_manifest(self):
        self.copy_rc = 1
        original = LAB.execute.side_effect
        def execute(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ['skopeo', 'copy']: self.current_raw = self.new_raw
            return result
        LAB.execute.side_effect = execute
        with self.assertRaisesRegex(ValueError, 'explicit recovery'): self.run_repair()
        LAB.execute.side_effect = original; self.copy_rc = 0
        self.run_repair(restore=True)
        self.assertEqual(self.current_raw, self.old_raw)

    def test_rollback_without_matching_receipt_refused(self):
        self.current_raw = self.new_raw
        with self.assertRaisesRegex(ValueError, 'matching forward receipt'): self.run_repair(restore=True)
        self.assertEqual(self.copies(), [])


class RuntimeCacheReconcileContracts(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('runtime_monitoring', ROOT/'tests/helpers/longhorn-v2-monitoring.py')
        self.monitoring = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.monitoring)
        raw = b'{"key":"brinnatt/busybox:1.37"}'
        self.raw = raw
        self.environment = {
            'LONGHORN_MONITORING_CACHE_RECONCILE_FOCUS': 'v2-observability-monitoring',
            'LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD': base64.b64encode(raw).decode(),
            'LONGHORN_MONITORING_CACHE_RECONCILE_SHA256': hashlib.sha256(raw).hexdigest()}

    def test_default_setup_does_not_consume_or_repair_runtime_proof(self):
        with patch.dict(os.environ, self.environment):
            self.assertIsNone(self.monitoring.runtime_cache_proof('setup', False))

    def test_explicit_focused_setup_reads_exact_hash_bound_json(self):
        with patch.dict(os.environ, self.environment):
            self.assertEqual(self.monitoring.runtime_cache_proof('setup', True), json.loads(self.raw))

    def test_existing_main_forwards_runtime_proof_only_to_explicit_setup(self):
        from types import SimpleNamespace
        lab=SimpleNamespace(require_lease=Mock())
        fixture=SimpleNamespace(fixture=Mock(return_value=(None,lab)))
        with patch.dict(os.environ,self.environment), patch.object(sys,'argv',[
                'longhorn-v2-monitoring.py','setup','--relay-pid','12','--reconcile-cache']), \
                patch.object(self.monitoring,'load',return_value=fixture), \
                patch.object(self.monitoring,'setup') as setup:
            self.monitoring.main()
            setup.assert_called_once_with(lab,12,cache_reconcile=json.loads(self.raw))
            lab.require_lease.assert_called_once()
        with patch.dict(os.environ,self.environment), patch.object(sys,'argv',[
                'longhorn-v2-monitoring.py','cleanup','--reconcile-cache']), \
                patch.object(self.monitoring,'load') as load:
            with self.assertRaises(ValueError): self.monitoring.main()
            load.assert_not_called()

    def test_runtime_wrong_action_focus_base64_size_json_or_hash_rejected(self):
        variants = [dict(self.environment, LONGHORN_MONITORING_CACHE_RECONCILE_FOCUS='v2-observability'),
            dict(self.environment, LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD='%%%'),
            dict(self.environment, LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD='A'*43693),
            dict(self.environment, LONGHORN_MONITORING_CACHE_RECONCILE_SHA256='0'*64),
            dict(self.environment, LONGHORN_MONITORING_CACHE_RECONCILE_SHA256='invalid')]
        for raw in [b'{', b'[]', b'\xff', b' '*32769]:
            variants.append(dict(self.environment, LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD=
                base64.b64encode(raw).decode(), LONGHORN_MONITORING_CACHE_RECONCILE_SHA256=hashlib.sha256(raw).hexdigest()))
        for environment in variants:
            with self.subTest(environment_keys=list(environment)), patch.dict(os.environ, environment):
                with self.assertRaises(ValueError): self.monitoring.runtime_cache_proof('setup', True)
        for action in ['verify', 'cleanup', 'admission']:
            with self.subTest(action=action), patch.dict(os.environ, self.environment):
                with self.assertRaises(ValueError): self.monitoring.runtime_cache_proof(action, True)

    def launch_protocol(self, raw=None, module='v2-observability-monitoring', bad_sha=False, symlink=False, original_wrappers=False):
        raw = self.raw if raw is None else raw
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        binary = root/'.venv/bin/python'; binary.parent.mkdir(parents=True); binary.symlink_to(sys.executable)
        sync = root/'tests/helpers/sync-kubeauto.sh'; sync.parent.mkdir(parents=True); sync.write_text('#!/bin/bash\nexit 0\n')
        (sync.parent/'longhorn-lab.py').write_text('import sys\nassert sys.argv[1:] == [\"verify-transport\"]\n')
        proof = root/'proof.json'; proof.write_bytes(raw)
        if symlink:
            link = root/'proof-link.json'; link.symlink_to(proof); proof = link
        trace = root/'protocol.trace'
        script = r"""
set -euo pipefail
source "$CANDIDATE"
longhorn_local_gate() {
  [[ -z "${LONGHORN_MONITORING_CACHE_RECONCILE_PROOF+x}" &&
     -z "${LONGHORN_MONITORING_CACHE_RECONCILE_SHA256+x}" ]] || return 86
  if [[ "$1" == static && "$ORIGINAL_WRAPPERS" == yes ]]; then
    "$ORIGINAL_PYTHON" -m unittest tests.unit.test_longhorn_v2_observability tests.unit.test_longhorn_node_lifecycle_shell -v || return
  fi
}
monitor_remote_job() { return 0; }
remote_job_summary() { printf '{}\n'; }
ssh_longhorn() {
  if [[ "${1:-}" == $'set -eu\n'* ]]; then
    printf 'LONGHORN_LEASE_READY\n'
    IFS= read -r mode
    IFS= read -r payload
    IFS= read -r sha
    printf '%s\n%s\n%s\n' "$mode" "$payload" "$sha" > "$TRACE"
    printf 'LONGHORN_DURABLE_LAUNCHED\n'
  else
    case "${1:-}" in *live.log*) printf 'LONGHORN_V2_OBSERVABILITY_MONITORING_PASS\n';; *) printf '0\n';; esac
  fi
}
longhorn_launch "$MODULE" LONGHORN_V2_OBSERVABILITY_MONITORING_PASS
"""
        environment = dict(os.environ, ROOT=str(root), TRACE=str(trace), MODULE=module,
            CANDIDATE=str(ROOT/'tests/helpers/longhorn-runner.sh'),
            ORIGINAL_WRAPPERS='yes' if original_wrappers else 'no', ORIGINAL_PYTHON=sys.executable, PYTHONPATH=str(ROOT),
            LONGHORN_MONITORING_CACHE_RECONCILE_PROOF=str(proof),
            LONGHORN_MONITORING_CACHE_RECONCILE_SHA256='0'*64 if bad_sha else hashlib.sha256(raw).hexdigest())
        result = subprocess.run(['bash', '-c', script], env=environment, capture_output=True, text=True, timeout=15)
        return result, trace

    def test_real_runtime_inputs_are_isolated_before_original_observability_and_node_preflight(self):
        result,trace=self.launch_protocol(module='v1-operations-remaining-v2-monitoring',original_wrappers=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('OK',result.stderr)
        mode,payload,sha=trace.read_text().splitlines()
        self.assertEqual(mode,'v1-operations-remaining-v2-monitoring')
        self.assertEqual(base64.b64decode(payload,validate=True),self.raw)
        self.assertEqual(sha,hashlib.sha256(self.raw).hexdigest())

    def test_existing_lease_protocol_carries_exact_proof_and_sha_without_remote_staging(self):
        result, trace = self.launch_protocol()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        mode, encoded, sha = trace.read_text().splitlines()
        self.assertEqual(mode, 'v2-observability-monitoring')
        self.assertEqual(base64.b64decode(encoded, validate=True), self.raw)
        self.assertEqual(sha, hashlib.sha256(self.raw).hexdigest())

    def test_existing_combined_parent_carries_same_proof_only_to_original_v2_monitoring_scope(self):
        result,trace=self.launch_protocol(module='v1-operations-remaining-v2-monitoring')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        mode,encoded,sha=trace.read_text().splitlines()
        self.assertEqual(mode,'v1-operations-remaining-v2-monitoring')
        self.assertEqual(base64.b64decode(encoded,validate=True),self.raw)
        self.assertEqual(sha,hashlib.sha256(self.raw).hexdigest())
        # The existing combined profile owns two independent children. Only
        # its original monitoring child ever supplies --reconcile-cache.
        source=(ROOT/'tests/helpers/longhorn-parallel.sh').read_text()
        profile=source.split('  v1-operations-remaining-v2-monitoring)',1)[1].split('    ;;',1)[0]
        self.assertIn('v1_mode=v1-operations-remaining-recovery',profile)
        self.assertIn('v2_mode=v2-observability-monitoring',profile)
        self.assertNotIn('--reconcile-cache',profile)

    def test_local_bad_json_size_sha_symlink_or_wrong_mode_reject_before_remote_handshake(self):
        for options in [{'raw': b'{'}, {'raw': b'[]'}, {'raw': b'x'*32769}, {'bad_sha': True},
                        {'symlink': True}, {'module': 'v2-observability'}, {'module': 'v1-stability-v2-observability'}]:
            with self.subTest(options={k:len(v) if isinstance(v, bytes) else v for k,v in options.items()}):
                result, trace = self.launch_protocol(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(trace.exists(), result.stdout+result.stderr)

    def test_single_focus_maps_to_existing_owning_wrapper_and_static_gate(self):
        script = r'''
source "$CANDIDATE"
longhorn_launch() { printf '%s %s\n' "$1" "$2"; }
longhorn_dispatch --longhorn-v2-observability-monitoring
'''
        result = subprocess.run(['bash','-c',script], env=dict(os.environ,
            CANDIDATE=str(ROOT/'tests/helpers/longhorn-runner.sh')), capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'v2-observability-monitoring LONGHORN_V2_OBSERVABILITY_MONITORING_PASS')
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertIn('v2-observability|v2-observability-monitoring|v2-observability-business)',live)
        self.assertIn('tests.unit.test_longhorn_monitoring_cache_reconcile -v',
            (ROOT/'tests/helpers/longhorn-static.sh').read_text())

    def test_proof_reaches_only_focused_setup_and_cpu_is_reused(self):
        fake=r'''#!/usr/bin/env bash
set -eu
helper="${1##*/}"; action="$2"
printf '%s %s %s\n' "$helper" "$action" "${*:3}" >> "$TRACE"
case "$helper $action" in
 'longhorn-v2-install.py relay-maintain') trap 'exit 0' TERM; while :; do sleep 0.02; done;;
 'longhorn-v2-cpu-isolation.py business') exit 99;;
 'longhorn-v2-cpu-isolation.py verify-completed') echo LONGHORN_V2_CPU_ISOLATION_REUSE_PASS;;
 'longhorn-v2-monitoring.py setup') [[ "${*:3}" == *--reconcile-cache* ]] || exit 98; echo LONGHORN_V2_MONITORING_SETUP_PASS;;
 'longhorn-v2-observability.py business') echo LONGHORN_V2_OBSERVABILITY_BUSINESS_PASS;;
esac
'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); helpers=root/'tests/helpers';helpers.mkdir(parents=True)
            binary=root/'.venv/bin/python';binary.parent.mkdir(parents=True);binary.write_text(fake);binary.chmod(0o700)
            original=ROOT/'tests/helpers/run-durable-gate.sh'
            (helpers/'run-durable-gate.sh').write_text(original.read_text())
            source=(ROOT/'tests/helpers/longhorn-v2-observability.sh').read_text().replace('/usr/local/kubeauto',str(root))
            source=source.replace('/var/tmp/kubeauto-longhorn-v2-observability.',str(root/'durable.'))
            wrapper=helpers/'wrapper.sh';wrapper.write_text(source)
            binary=root/'kube-bin/kubectl';binary.parent.mkdir();binary.write_text('#!/bin/bash\nexit 0\n');binary.chmod(0o700)
            trace=root/'trace';environment=dict(os.environ,**self.environment,TRACE=str(trace),LONGHORN_PARENT_LEASE_HELD='yes')
            result=subprocess.run(['bash',str(wrapper),'v2-observability-monitoring'],env=environment,
                capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            lines=trace.read_text().splitlines()
            self.assertFalse(any(line.startswith('longhorn-v2-cpu-isolation.py business') for line in lines))
            self.assertTrue(any(line.startswith('longhorn-v2-cpu-isolation.py verify-completed') for line in lines))
            self.assertEqual(sum('--reconcile-cache' in line for line in lines),1)
            self.assertIn('LONGHORN_V2_OBSERVABILITY_MONITORING_PASS',result.stdout)
            self.assertEqual((next(root.glob('durable.*'))/'exit').read_text().strip(),'0')
            # The same runtime payload must never make the full wrapper repair.
            result=subprocess.run(['bash',str(wrapper),'v2-observability'],env=environment,
                capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,2,result.stdout+result.stderr)

if __name__ == '__main__':
    unittest.main()
