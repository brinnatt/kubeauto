"""Hugepage ownership, partial-write recovery and native resource admission."""
import copy
from contextlib import redirect_stdout
import io
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
SPEC = importlib.util.spec_from_file_location('v2_resources', ROOT/'tests/helpers/longhorn-v2-resources.py')
RES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RES)
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')


def host(pages=0):
    return {'machine_id': 'original-machine', 'boot_id': 'original-boot', 'total': pages,
            'free': pages, 'reserved': 0, 'available_kib': 6*1024**2, 'kubelet_active': 'active'}


class ResourceContracts(unittest.TestCase):
    def lab(self, directory):
        state = {'run_id': 'original-run', 'cluster_uid': 'original-cluster',
                 'raw_disks': {node: {'machine_id': 'original-machine'} for node in NODES}}
        def write(path, row):
            path.write_text(json.dumps(row))
        return SimpleNamespace(DIRECTORY=Path(directory), NODES=NODES, owner=lambda: state,
            core_identity=MagicMock(), verify_core_smoke=MagicMock(), get=MagicMock(return_value=None),
            write_private=write)

    def test_installation_resource_prerequisite_requires_pass_and_unchanged_causal_inputs(self):
        names = ('tests/helpers/longhorn-v2-resources.py', 'tests/helpers/longhorn-v2-resources.sh', 'roles/longhorn/files/longhorn.py')
        for failure in ('none', 'status', 'rc', 'failure-markers', *names):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); inputs = {}
                for name in names:
                    file = base/name; file.parent.mkdir(parents=True, exist_ok=True); file.write_text(name)
                    inputs[name] = hashlib.sha256(file.read_bytes()).hexdigest()
                evidence = {'status': 'pass', 'rc': 0, 'failure_markers': 0, 'frozen_inputs': inputs}
                if failure == 'status': evidence['status'] = 'pending'
                if failure == 'rc': evidence['rc'] = 1
                if failure == 'failure-markers': evidence['failure_markers'] = 1
                if failure in names: (base/failure).write_text('changed')
                path = base/'tests/evidence/longhorn-v2-resources-qualification-20261020.json'
                path.parent.mkdir(parents=True); path.write_text(json.dumps(evidence))
                with patch.object(RES, 'BASE', base):
                    if failure == 'none': RES.qualification_current()
                    else:
                        with self.assertRaises(ValueError): RES.qualification_current()

    def failed_write(self, lab, base):
        with patch.object(RES, 'probe', side_effect=[host()]*3+[OSError('lost')]):
            with self.assertRaises(OSError): RES.prepare(lab)
        raw = RES.record_path(lab).read_bytes()
        record = RES.read_record(lab)
        writer = {'pid': 99999999, 'boot_id': 'original-boot',
                  'command_sha256': hashlib.sha256(b'python3\0-c\0'+RES.host_script().encode()+b'\0').hexdigest()}
        evidence = {'status': 'fail', 'rc': 1, 'resource_journal_sha256': hashlib.sha256(raw).hexdigest(),
                    'resource_journal_id': record['id'], 'allocation_observation': {'host': NODES[0], 'writer': writer}}
        path = base/'tests/evidence/longhorn-v2-resources-allocation-first-failure-20261020.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(evidence))
        return path, evidence, raw

    def test_diagnosed_completion_checks_all_hosts_before_confirming_then_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); lab = self.lab(directory)
            _, _, raw = self.failed_write(lab, base)
            lab.remote = MagicMock(return_value=SimpleNamespace(stdout='writer_absent'))
            with patch.object(RES, 'BASE', base), patch.object(RES, 'probe', side_effect=[host(1024), host(), host()]):
                RES.resolve_completed_write(lab)
            record = RES.read_record(lab)
            self.assertEqual(record['hosts'][NODES[0]]['phase'], 'write-confirmed')
            archive = next(base.glob('*ambiguous-*.json'))
            self.assertEqual(json.loads(archive.read_text()), json.loads(raw))
            with patch.object(RES, 'probe', side_effect=[host(1024), host(), host(), host()]), patch.object(RES, 'refresh_kubelet'):
                RES.restore(lab)
            self.assertFalse(RES.record_path(lab).exists())

    def test_diagnosed_completion_refuses_changed_evidence_or_live_state_without_writes(self):
        for failure in ('digest', 'id', 'command', 'boot', 'phase', 'writer', 'partial', 'busy', 'other-host', 'reboot'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); lab = self.lab(directory)
                path, evidence, raw = self.failed_write(lab, base)
                if failure == 'digest': evidence['resource_journal_sha256'] = 'foreign'
                if failure == 'id': evidence['resource_journal_id'] = 'foreign'
                if failure == 'command': evidence['allocation_observation']['writer']['command_sha256'] = 'foreign'
                if failure == 'boot': evidence['allocation_observation']['writer']['boot_id'] = 'foreign'
                if failure == 'phase': evidence['allocation_observation']['host'] = NODES[1]
                path.write_text(json.dumps(evidence))
                rows = [host(1024), host(), host()]
                if failure == 'partial': rows[0] = host(512)
                if failure == 'busy': rows[0]['free'] = 1023
                if failure == 'other-host': rows[1] = host(1024)
                if failure == 'reboot': rows[2]['boot_id'] = 'rebooted'
                lab.remote = MagicMock(return_value=SimpleNamespace(stdout='writer_present' if failure == 'writer' else 'writer_absent'))
                with patch.object(RES, 'BASE', base), patch.object(RES, 'probe', side_effect=rows):
                    with self.assertRaises(ValueError): RES.resolve_completed_write(lab)
                self.assertEqual(RES.record_path(lab).read_bytes(), raw)
                self.assertFalse(list(base.glob('*ambiguous-*.json')))

    def test_remote_writer_proof_rejects_live_reused_pid_and_duplicate_program(self):
        for failure in ('absent', 'pid', 'duplicate', 'boot'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); lab = self.lab(directory)
                _, evidence, _ = self.failed_write(lab, base)
                proc = base/'proc'; (proc/'sys/kernel/random').mkdir(parents=True)
                (proc/'sys/kernel/random/boot_id').write_text('wrong' if failure == 'boot' else 'original-boot')
                if failure == 'pid': (proc/'99999999').mkdir()
                if failure == 'duplicate':
                    (proc/'123').mkdir()
                    (proc/'123/cmdline').write_bytes(b'python3\0-c\0'+RES.host_script().encode()+b'\0')
                def remote(node, argv, *, stdin):
                    script = argv[-1].replace('/proc', str(proc))
                    result = subprocess.run([sys.executable, '-c', script], input=stdin, text=True, capture_output=True)
                    if result.returncode: raise ValueError('writer proof refused')
                    return result
                lab.remote = remote
                with patch.object(RES, 'BASE', base), patch.object(RES, 'probe', side_effect=[host(1024), host(), host()]):
                    if failure == 'absent': RES.resolve_completed_write(lab)
                    else:
                        with self.assertRaisesRegex(ValueError, 'proof refused'): RES.resolve_completed_write(lab)

    def test_resize_uses_bounded_long_wait_and_read_only_probe_keeps_short_timeout(self):
        lab = SimpleNamespace(remote=MagicMock(return_value=SimpleNamespace(stdout=json.dumps(host()))))
        RES.probe(lab, NODES[0]); self.assertEqual(lab.remote.call_args.kwargs['timeout'], 120)
        RES.probe(lab, NODES[0], target=1024, expected=host())
        self.assertEqual(lab.remote.call_args.kwargs['timeout'], 600)

    def test_every_host_is_qualified_before_journal_or_remote_mutation(self):
        for failure in ('foreign-machine', 'busy-pool', 'existing-pool', 'memory', 'stopped-kubelet'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                lab = self.lab(directory)
                bad = host()
                if failure == 'foreign-machine': bad['machine_id'] = 'foreign'
                if failure == 'busy-pool': bad['reserved'] = 1
                if failure == 'existing-pool': bad.update(total=1024, free=1024)
                if failure == 'memory': bad['available_kib'] = 1024
                if failure == 'stopped-kubelet': bad['kubelet_active'] = 'inactive'
                with patch.object(RES, 'probe', side_effect=[host(), host(), bad]) as probe:
                    with self.assertRaises(ValueError): RES.prepare(lab)
                self.assertTrue(all('target' not in call.kwargs for call in probe.call_args_list))
                self.assertFalse(RES.record_path(lab).exists())

    def test_memory_refusal_reports_node_and_actual_budget_without_new_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = self.lab(directory); row = host(); row['available_kib'] = 2325040
            with patch.object(RES, 'probe', return_value=row) as probe, redirect_stdout(io.StringIO()) as output:
                with self.assertRaisesRegex(ValueError, 'node=192.168.47.146'): RES.prepare(lab)
            self.assertIn('available_kib=2325040 required_available_kib=3145728', output.getvalue())
            self.assertEqual(probe.call_count, 1)
            self.assertFalse(RES.record_path(lab).exists())

    def test_prepare_and_restore_bind_exact_host_state_and_refresh_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = self.lab(directory)
            pools = {node: host() for node in NODES}
            calls = []
            def probe(_, node, *, target=None, expected=None):
                if target is not None:
                    self.assertEqual(expected, pools[node])
                    pools[node] = host(target)
                    calls.append((node, target))
                return copy.deepcopy(pools[node])
            with patch.object(RES, 'probe', side_effect=probe), patch.object(RES, 'refresh_kubelet') as refresh:
                RES.prepare(lab)
                self.assertEqual([value for _, value in calls], [1024]*3)
                self.assertTrue(all(row['phase'] == 'ready' for row in RES.read_record(lab)['hosts'].values()))
                RES.restore(lab)
                self.assertEqual([value for _, value in calls], [1024]*3+[0]*3)
                self.assertEqual(refresh.call_count, 6)
                self.assertFalse(RES.record_path(lab).exists())
                self.assertEqual(len(list(Path(directory).glob('*restored-*.json'))), 1)

    def test_ambiguous_write_or_changed_identity_pool_and_consumers_block_all_recovery(self):
        for failure in ('ambiguous', 'machine', 'boot', 'pages', 'busy', 'namespace', 'cluster'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                lab = self.lab(directory)
                with patch.object(RES, 'probe', side_effect=[host()]*3+[host(1024)]*3), patch.object(RES, 'refresh_kubelet'):
                    RES.prepare(lab)
                record = RES.read_record(lab)
                actual = host(1024)
                if failure == 'ambiguous': record['hosts'][NODES[1]]['phase'] = 'write-intent'
                if failure == 'machine': actual['machine_id'] = 'foreign'
                if failure == 'boot': actual['boot_id'] = 'rebooted'
                if failure == 'pages': actual.update(total=2048, free=2048)
                if failure == 'busy': actual['free'] = 1023
                if failure == 'namespace': lab.get.return_value = {'metadata': {'uid': 'foreign'}}
                if failure == 'cluster': record['cluster_uid'] = 'foreign'
                lab.write_private(RES.record_path(lab), record)
                with patch.object(RES, 'probe', return_value=actual) as probe, patch.object(RES, 'refresh_kubelet') as refresh:
                    with self.assertRaises(ValueError): RES.restore(lab)
                self.assertTrue(all('target' not in call.kwargs for call in probe.call_args_list))
                refresh.assert_not_called()
                self.assertTrue(RES.record_path(lab).is_file())

    def test_partial_allocation_response_is_journaled_and_can_be_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = self.lab(directory)
            with patch.object(RES, 'probe', side_effect=[host()]*3+[host(512)]), patch.object(RES, 'refresh_kubelet'):
                with self.assertRaisesRegex(ValueError, 'complete V2 page pool'): RES.prepare(lab)
            row = RES.read_record(lab)
            self.assertEqual(row['hosts'][NODES[0]]['current']['total'], 512)
            self.assertEqual(row['hosts'][NODES[0]]['phase'], 'write-confirmed')
            with patch.object(RES, 'probe', side_effect=[host(512), host(), host(), host()]), patch.object(RES, 'refresh_kubelet'):
                RES.restore(lab)
            self.assertFalse(RES.record_path(lab).exists())

    def test_lost_write_response_preserves_intent_and_refuses_automatic_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = self.lab(directory)
            with patch.object(RES, 'probe', side_effect=[host()]*3+[OSError('response lost')]):
                with self.assertRaises(OSError): RES.prepare(lab)
            self.assertEqual(RES.read_record(lab)['hosts'][NODES[0]]['phase'], 'write-intent')
            with patch.object(RES, 'probe') as probe:
                with self.assertRaisesRegex(ValueError, 'ambiguous'): RES.restore(lab)
            probe.assert_not_called()

    def test_tampered_original_target_or_phase_refuses_all_remote_recovery(self):
        for failure in ('original', 'target', 'phase'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                lab = self.lab(directory)
                with patch.object(RES, 'probe', side_effect=[host()]*3+[host(1024)]*3), patch.object(RES, 'refresh_kubelet'):
                    RES.prepare(lab)
                record = RES.read_record(lab)
                row = record['hosts'][NODES[0]]
                if failure == 'original': row['original'].update(total=1024, free=1024)
                if failure == 'target': row['current'].update(total=2048, free=2048)
                if failure == 'phase': row['phase'] = 'restored'
                lab.write_private(RES.record_path(lab), record)
                with patch.object(RES, 'probe') as probe:
                    with self.assertRaisesRegex(ValueError, 'journal'): RES.restore(lab)
                probe.assert_not_called()

    def test_host_program_enforces_compare_before_write_and_reports_partial_result(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pages = base/'pages'; pages.mkdir()
            (pages/'nr_hugepages').write_text('0'); (pages/'free_hugepages').write_text('0')
            (base/'machine-id').write_text('original-machine'); (base/'boot-id').write_text('original-boot')
            (base/'meminfo').write_text('MemAvailable: 6291456 kB\nHugePages_Rsvd: 0\n')
            script = RES.host_script()
            for old, new in [('/sys/kernel/mm/hugepages/hugepages-2048kB', pages),
                             ('/etc/machine-id', base/'machine-id'),
                             ('/proc/sys/kernel/random/boot_id', base/'boot-id'),
                             ('/proc/meminfo', base/'meminfo')]:
                script = script.replace(old, str(new))
            script = script.replace("subprocess.run(['systemctl','is-active','kubelet'],text=True,capture_output=True,timeout=15).stdout.strip()", "'active'")
            def run(expected, target):
                return subprocess.run([sys.executable, '-c', script], input=json.dumps(
                    {'action': 'resize', 'expected': expected, 'target': target}), text=True, capture_output=True, timeout=5)
            expected = host(); expected['boot_id'] = 'wrong-boot'
            self.assertNotEqual(run(expected, 1024).returncode, 0)
            self.assertEqual((pages/'nr_hugepages').read_text(), '0')
            self.assertNotEqual(run(host(), 4096).returncode, 0)
            result = run(host(), 1024)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['total'], 1024)

    def test_native_product_admission_rejects_missing_kubelet_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = self.lab(directory)
            with patch.object(RES, 'probe', side_effect=[host()]*3+[host(1024)]*3), patch.object(RES, 'refresh_kubelet'):
                RES.prepare(lab)
            def get(kind, name):
                return {'status': {'allocatable': {'cpu': '8', 'memory': '6Gi', 'hugepages-2Mi': '0'}}}
            lab.get = get
            with patch.object(RES, 'BASE', ROOT), patch.object(RES, 'probe', return_value=host(1024)):
                with self.assertRaisesRegex(ValueError, 'kubelet hugepages'): RES.verify(lab)

    def test_fixed_dispatch_and_both_remote_allowlists_include_resource_qualification(self):
        path = ROOT/'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-v2-resources-qualification\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(path)], text=True,
                                capture_output=True, check=True)
        self.assertEqual(result.stdout, 'v2-resources-qualification LONGHORN_V2_RESOURCES_QUALIFICATION_PASS')
        for variable in ('module', 'mode'):
            branches = re.findall(r'case "\$' + variable + r'" in ([^\n]+)', path.read_text())
            self.assertEqual(len(branches), 1)
            self.assertIn('v2-resources-qualification', branches[0].split(')')[0].split('|'))
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-resources.sh" "$MODE"',
                      (ROOT/'tests/helpers/longhorn-live.sh').read_text())

    def test_recovery_dispatch_and_allowlists(self):
        path = ROOT/'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-v2-resources-recovery\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(path)], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, 'v2-resources-recovery LONGHORN_V2_RESOURCES_RECOVERY_PASS')
        for variable in ('module', 'mode'):
            branches = re.findall(r'case "\$' + variable + r'" in ([^\n]+)', path.read_text())
            self.assertEqual(len(branches), 1)
            self.assertIn('v2-resources-recovery', branches[0].split(')')[0].split('|'))

    def test_shell_always_restores_resources_and_transport_preserving_first_failure(self):
        source = (ROOT/'tests/helpers/longhorn-v2-resources.sh').read_text()
        for mode, failure in [(mode, failure) for mode in ('qualification', 'recovery') for failure in ('none', 'relay-ensure', 'restore', 'smoke', 'relay-clean') + (('prepare', 'verify') if mode == 'qualification' else ('resolve-completed-write',))]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'tests/helpers').mkdir(parents=True)
                (base/'kube-bin').mkdir()
                (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n')
                (base/'kube-bin/kubectl').chmod(0o755)
                helper = '''import os,signal,sys,time
mode=sys.argv[1]
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True: time.sleep(.01)
with open('calls.txt','a') as stream: stream.write(mode+'\\n')
if mode==os.environ['FAIL_MODE']: sys.exit(17)
'''
                for name in ('longhorn-v2-core.py', 'longhorn-v2-resources.py'):
                    (base/'tests/helpers'/name).write_text(helper)
                text = source.replace('BASE=/usr/local/kubeauto', 'BASE=' + str(base)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-resources.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', text, 'v2-resource-contract', 'v2-resources-'+mode],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure),
                    text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertIn('restore', calls)
                self.assertIn('relay-clean', calls)
                if failure == 'none':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('LONGHORN_V2_RESOURCES_'+mode.upper()+'_PASS', result.stdout)
                    if mode == 'recovery':
                        self.assertLess(calls.index('resolve-completed-write'), calls.index('restore'))
                        self.assertNotIn('prepare', calls)
                    self.assertEqual(calls.index('restore') < calls.index('smoke'), True)
                else:
                    self.assertEqual(result.returncode, 17, result.stderr)
                    self.assertNotIn('LONGHORN_V2_RESOURCES_'+mode.upper()+'_PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
