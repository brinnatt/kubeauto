"""Bounded RT/cgroup environment recovery; never a Kubernetes product PASS."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('anolis_cpu_fixture', ROOT / 'tests/helpers/lab-anolis-cgroup.py')
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


class AnolisCgroupFixtureContracts(unittest.TestCase):
    def test_kernel_write_failure_identifies_operation_and_retains_recovery_journal(self):
        for operation in ('cgroup.procs', 'cgroup.subtree_control'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'cgroup.controllers').write_text('cpu memory')
                (base/'cgroup.subtree_control').write_text('memory')
                proc = base/'123'; proc.mkdir()
                (proc/'cgroup').write_text('0::/system.slice/rt.service')
                row = {'pid':123, 'starttime':'1', 'cgroup':'/system.slice/rt.service'}
                original_write = Path.write_text

                def fail_write(path, value, *args, **kwargs):
                    if path == base/operation:
                        raise OSError(22, 'Invalid argument')
                    return original_write(path, value, *args, **kwargs)

                with (
                    patch.object(FIXTURE, 'CGROUP', base), patch.object(FIXTURE, 'PROC', base),
                    patch.object(FIXTURE, 'JOURNAL', base/'journal'),
                    patch.object(FIXTURE, 'boot_id', return_value='boot-1'),
                    patch.object(FIXTURE, 'require_idle'),
                    patch.object(FIXTURE, 'process_identity', return_value='1'),
                    patch.object(FIXTURE, 'realtime_groups', side_effect=[[row], []]),
                    patch.object(Path, 'write_text', fail_write),
                ):
                    message = 'RT migration failed for pid=123' if operation == 'cgroup.procs' else 'CPU controller enable failed'
                    with self.assertRaisesRegex(RuntimeError, message):
                        FIXTURE.prepare('owned-run')
                    self.assertEqual(json.loads((base/'journal').read_text())['run_id'], 'owned-run')

    def test_wrapper_preserves_first_failure_and_restores_only_owned_fixture(self):
        for failure in ('', 'prepare', 'partial-prepare', 'foreign', 'dirty', 'restore', 'lost-journal'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                helpers = base / 'tests/helpers'; helpers.mkdir(parents=True)
                (helpers / 'anolis-cgroup-qualification.sh').write_bytes((ROOT / 'tests/helpers/anolis-cgroup-qualification.sh').read_bytes())
                (helpers / 'lab-anolis-cgroup.py').touch()
                (helpers / 'lab-wipe-nodes.sh').write_text('#!/bin/sh\n[ "$CASE" != dirty ]\n')
                if failure == 'foreign':
                    (base / 'journal').touch()
                executable = base / 'bin'; executable.mkdir()
                ssh = executable / 'ssh'
                ssh.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
base=pathlib.Path(os.environ['FAKE_ROOT']); case=os.environ['CASE']; command=sys.argv[-1]
assert 'root@192.168.47.141' in sys.argv
with (base/'trace').open('a') as out: out.write(command+'\\n')
journal=base/'journal'
if command.startswith('test !'): sys.exit(1 if journal.exists() else 0)
if command.startswith('test -f'): sys.exit(0 if journal.exists() else 1)
if command.startswith('python3 '):
    if not (base/'remote-path').exists() or (base/'remote-path').read_text()!=command.split()[1]: sys.exit(2)
if ' prepare ' in command:
    if case=='prepare': sys.exit(23)
    journal.touch()
    if case=='partial-prepare': sys.exit(23)
    if case=='lost-journal': journal.unlink()
    print('ANOLIS_CPU_PREREQUISITE_PASS rt_processes=2'); sys.exit(0)
if command=='systemctl reboot':
    (base/'newboot').touch()
    if (base/'remote-path').read_text().startswith('/tmp/'): (base/'remote-path').unlink()
    sys.exit(0)
if ' restore ' in command:
    if case=='restore': sys.exit(17)
    if not (base/'newboot').exists():
        print('ANOLIS_CPU_RESTORE_REBOOT_REQUIRED'); sys.exit(42)
    journal.unlink(); print('ANOLIS_CPU_RESTORE_PASS'); sys.exit(0)
if command.startswith('rm -f '): (base/'remote-path').unlink(); sys.exit(0)
sys.exit(99)
''')
                ssh.chmod(0o755)
                scp = executable / 'scp'
                scp.write_text("#!/usr/bin/env python3\nimport os,pathlib,sys\npathlib.Path(os.environ['FAKE_ROOT'],'remote-path').write_text(sys.argv[-1].split(':',1)[1])\n")
                scp.chmod(0o755)
                sleep = executable / 'sleep'; sleep.write_text('#!/bin/sh\nexit 0\n'); sleep.chmod(0o755)
                result = subprocess.run(['bash', str(helpers / 'anolis-cgroup-qualification.sh')],
                    env={**os.environ, 'FAKE_ROOT': str(base), 'CASE': failure,
                         'PATH': str(executable) + ':' + os.environ['PATH']},
                    capture_output=True, text=True, timeout=15)
                trace = (base / 'trace').read_text() if (base / 'trace').exists() else ''
                if not failure:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('ANOLIS_CPU_QUALIFICATION_PASS', result.stdout)
                    self.assertLess(trace.index(' prepare '), trace.index(' restore '))
                    self.assertLess(trace.index(' restore '), trace.index('systemctl reboot'))
                    self.assertFalse((base / 'journal').exists())
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('ANOLIS_CPU_QUALIFICATION_PASS', result.stdout)
                    if failure == 'partial-prepare':
                        self.assertEqual(result.returncode, 23)
                        self.assertIn('ANOLIS_CPU_RESTORE_PASS', result.stdout)
                        self.assertFalse((base / 'journal').exists())
                    if failure in ('foreign', 'dirty', 'prepare', 'restore'):
                        self.assertNotIn('systemctl reboot', trace)

    def test_prepare_journals_before_migration_and_restore_requires_clean_new_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            proc = base / 'proc'; proc.mkdir()
            cgroup = base / 'cgroup'; cgroup.mkdir()
            journal = base / 'journal'
            (cgroup / 'cgroup.controllers').write_text('cpu memory')
            (cgroup / 'cgroup.subtree_control').write_text('memory')
            tasks = proc / '123/task'; tasks.mkdir(parents=True)
            (tasks / '123').mkdir(); (tasks / '124').mkdir()
            (proc / '123/cgroup').write_text('0::/system.slice/rt.service\n')
            (proc / '123/stat').write_text('123 (RT process) ' + ' '.join(['S'] + ['0'] * 18 + ['12345']))
            write = Path.write_text
            migrations = []

            def kernel_write(path, text, *args, **kwargs):
                if path == cgroup / 'cgroup.procs':
                    saved = json.loads(journal.read_text())
                    self.assertEqual(saved['boot_id'], 'boot-1')
                    self.assertEqual(saved['rt_processes'][0]['starttime'], '12345')
                    self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
                    migrations.append(text)
                    return write(proc / text / 'cgroup', '0::/\n')
                if path == cgroup / 'cgroup.subtree_control' and text == '+cpu':
                    self.assertEqual(FIXTURE.realtime_groups(), [])
                    text = 'memory cpu'
                return write(path, text, *args, **kwargs)

            with (
                patch.object(FIXTURE, 'PROC', proc), patch.object(FIXTURE, 'CGROUP', cgroup),
                patch.object(FIXTURE, 'JOURNAL', journal), patch.object(FIXTURE, 'boot_id', return_value='boot-1') as boot,
                patch.object(FIXTURE, 'require_idle') as idle,
                patch.object(FIXTURE.os, 'sched_getscheduler', side_effect=lambda tid: os.SCHED_FIFO | os.SCHED_RESET_ON_FORK if tid == 124 else 0),
                patch.object(Path, 'write_text', kernel_write),
            ):
                FIXTURE.prepare('owned-run')
                self.assertEqual(migrations, ['123'])
                with self.assertRaisesRegex(RuntimeError, 'existing RT fixture'):
                    FIXTURE.prepare('owned-run')
                with self.assertRaisesRegex(RuntimeError, 'foreign RT fixture'):
                    FIXTURE.restore('foreign-run')
                self.assertEqual(FIXTURE.restore('owned-run'), 42)
                self.assertTrue(journal.exists())
                boot.return_value = 'boot-2'
                self.assertEqual(FIXTURE.restore('owned-run'), 0)
                self.assertFalse(journal.exists())
                self.assertEqual(idle.call_count, 5)

    def test_busy_node_is_refused_before_journal_or_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal'
            with (
                patch.object(FIXTURE, 'JOURNAL', journal),
                patch.object(FIXTURE, 'require_idle', side_effect=RuntimeError('Pod process remains')),
            ):
                with self.assertRaisesRegex(RuntimeError, 'Pod process remains'):
                    FIXTURE.prepare('owned-run')
                with self.assertRaisesRegex(RuntimeError, 'Pod process remains'):
                    FIXTURE.restore('owned-run')
            self.assertFalse(journal.exists())

    def test_other_hosts_are_refused_without_opening_node_lock(self):
        with (
            patch.object(FIXTURE.subprocess, 'run', return_value=SimpleNamespace(stdout='inet 192.168.47.143/24')),
            patch('builtins.open') as opener,
        ):
            with self.assertRaisesRegex(RuntimeError, 'restricted'):
                FIXTURE.main()
        opener.assert_not_called()

    def test_idle_guard_checks_services_and_pod_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / '321').mkdir()
            (proc / '321/cgroup').write_text('0::/kubepods.slice/pod-existing')
            with patch.object(FIXTURE, 'PROC', proc), patch.object(FIXTURE.subprocess, 'run') as command:
                command.return_value = SimpleNamespace(stdout='active\n')
                with self.assertRaisesRegex(RuntimeError, 'node service'):
                    FIXTURE.require_idle()
                command.return_value = SimpleNamespace(stdout='inactive\n')
                with self.assertRaisesRegex(RuntimeError, 'Pod process'):
                    FIXTURE.require_idle()


if __name__ == '__main__':
    unittest.main()
