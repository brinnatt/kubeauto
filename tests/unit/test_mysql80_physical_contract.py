"""Physical 8.0 gate admission, immutable fixture and real shell handoff contracts."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('mysql80_gate', ROOT / 'tests/helpers/mysql80-physical-live.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class Mysql80PhysicalContracts(unittest.TestCase):
    def test_mysqlbinlog_adapter_uses_client_equipped_pxb_and_keeps_arguments(self):
        text = (ROOT / 'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        adapter = text.split('cat >"$ROOT/bin/mysqlbinlog" <<EOF\n', 1)[1].split('\nEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'bin').mkdir()
            docker = root/'bin/docker'
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n'); docker.chmod(0o755)
            generated = subprocess.run(['bash','-euc', 'cat <<EOF\n'+adapter+'\nEOF'],
                env=dict(os.environ, ROOT=directory, MYSQL_IMAGE='server-without-client', PXB_IMAGE='pxb-with-client'),
                capture_output=True,text=True,check=True).stdout
            script=root/'mysqlbinlog'; script.write_text(generated)
            result=subprocess.run(['bash',script,'--start-position=125','file with spaces'],
                env=dict(os.environ,PATH=str(root/'bin')+':'+os.environ['PATH']),capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.splitlines()[-3:],['pxb-with-client','--start-position=125','file with spaces'])
        self.assertLess(text.index('mysqlbinlog --version'),text.index('docker run -d --name "$MYSQL_CTN"'))

    def test_time_filtered_replay_has_real_timezone_data_and_checks_offset(self):
        text=(ROOT/'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        replay=text.split('cat >"$ROOT/bin/mysqlbinlog" <<EOF\n',1)[1].split('\nEOF',1)[0]
        self.assertIn('/usr/share/zoneinfo/Asia/Shanghai:/usr/share/zoneinfo/Asia/Shanghai:ro',replay)
        self.assertIn('MYBACKUP_CLIENT_TIMEZONE_PASS offset=+0800',text)
        self.assertLess(text.index('MYBACKUP_CLIENT_TIMEZONE_PASS'),text.index('docker run -d --name "$MYSQL_CTN"'))

    def test_seed_selects_database_and_avoids_mysql_temporary_table_self_join(self):
        source = (ROOT / 'tests/helpers/mybackup-lifecycle.sh').read_text()
        seed = source.split('query <<SQL\n', 1)[1].split('\nSQL', 1)[0]
        self.assertIn('USE $MARKER;', seed)
        self.assertNotIn('CREATE TEMPORARY TABLE digits', seed)
        self.assertLess(seed.index('USE $MARKER;'), seed.index('CREATE TABLE digits'))
        self.assertIn('DROP TABLE digits;', seed)

    def test_real_full_launcher_refuses_missing_or_stale_documentation_before_ssh(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('tests/run_tools_regression.sh', 'tests/helpers/mysql80-physical-live.py', 'tests/tools-test-matrix.yaml'):
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            path = root / 'tests/tools-test-matrix.yaml'; matrix = yaml.safe_load(path.read_text())
            for case in matrix['test_cases']:
                if case['id'] in ('TL-MBK-01', 'TL-MBK-02', 'TL-MBK-04', 'TL-MBK-05',
                                  'TL-MBK-06', 'TL-MBK-07', 'TL-MBK-08'):
                    case['status'] = 'pass'
            path.write_text(yaml.safe_dump(matrix))
            def launch():
                result = subprocess.run(['bash', root / 'tests/run_tools_regression.sh', '--mysql-backup-live'],
                                        env=dict(os.environ, PYTHON=sys.executable, MYSQL_BACKUP_STAGE='physical80-full'),
                                        capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('ssh:', result.stderr)
                return result.stderr
            self.assertIn('MYSQL80_DOCUMENTATION_MISSING', launch())
            evidence = root / 'tests/evidence'; evidence.mkdir()
            review = evidence / 'mysql80-physical-documentation-20261125.json'
            review.write_text(json.dumps({'result': 'author-reviewed', 'checks': ['operations'],
                                          'files': {'manual.md': 'outdated'}}))
            (root / 'manual.md').write_text('current')
            self.assertIn('MYSQL80_DOCUMENTATION_STALE manual.md', launch())
            review.write_text(json.dumps({'result': 'author-reviewed', 'checks': ['operations'], 'files': {}}))
            self.assertIn('MYSQL80_DOCUMENTATION_REFUSED', launch())

    def test_real_full_launcher_refuses_pending_version_before_ssh(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('tests/run_tools_regression.sh', 'tests/helpers/mysql80-physical-live.py', 'tests/tools-test-matrix.yaml'):
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            path = root / 'tests/tools-test-matrix.yaml'; matrix = yaml.safe_load(path.read_text())
            for case in matrix['test_cases']:
                if case['id'] in ('TL-MBK-07', 'TL-MBK-08'): case['status'] = 'pending'
            path.write_text(yaml.safe_dump(matrix))
            result = subprocess.run(['bash', root / 'tests/run_tools_regression.sh', '--mysql-backup-live'],
                                    env=dict(os.environ, PYTHON=sys.executable, MYSQL_BACKUP_STAGE='physical80-full'),
                                    capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('TL-MBK-07', result.stderr)
        self.assertIn('TL-MBK-08', result.stderr)
        self.assertNotIn('ssh:', result.stderr)

    def test_changed_business_input_refuses_before_runtime_or_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir(); (root / 'dist').mkdir()
            (root / 'tests/tools-test-matrix.yaml').write_text('test_cases:\n' + ''.join(
                f'  - {{id: {name}, status: pass}}\n' for name in ('TL-MBK-01','TL-MBK-02','TL-MBK-04','TL-MBK-05','TL-MBK-06')))
            (root / 'tool.py').write_text('changed')
            (root / 'dist/mysql-backup-build.json').write_text(json.dumps({'inputs': {'tool.py': 'wrong'}}))
            with mock.patch.object(GATE, 'ROOT', root), self.assertRaisesRegex(ValueError, 'MYSQL80_BUILD_INPUT_STALE tool.py'):
                GATE.admit('8.0.33')

    def test_digest_fixture_switch_has_no_hardcoded_84_repository(self):
        text = (ROOT / 'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        setup = text.split('cleanup()', 1)[0]
        digest = 'sha256:' + 'a' * 64
        env = dict(os.environ, MYBACKUP_TOOL='/fixture/tool', MYBACKUP_VERSION='8.0.33',
                   MYBACKUP_MYSQL_IMAGE='hub.talkedu.cn/kubeauto/mysql:8.0.33',
                   MYBACKUP_MYSQL_DIGEST=digest,
                   MYBACKUP_PXB_IMAGE='hub.talkedu.cn/kubeauto/percona-xtrabackup:8.0.35-36',
                   MYBACKUP_PXB_DIGEST=digest)
        result = subprocess.run(['bash', '-euc', setup + '\nprintf "%s\\n" "$MYSQL_IMAGE" "$PXB_IMAGE" "$EXPECTED_VERSION"'],
                                env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), ['hub.talkedu.cn/kubeauto/mysql@' + digest,
            'hub.talkedu.cn/kubeauto/percona-xtrabackup@' + digest, '8.0.33'])
        self.assertIn('"${MYSQL_IMAGE%%@*}@$MYSQL_DIGEST"', text)

    def test_backup_adapter_preserves_configured_datadir_without_server_path_alias(self):
        source = (ROOT / 'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        block = source.split('cat >"$ROOT/bin/xtrabackup" <<EOF\n', 1)[1].split('\nEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'bin').mkdir()
            subprocess.run(['bash', '-c', 'cat >"$ROOT/bin/xtrabackup" <<EOF\n' + block + '\nEOF'],
                           env=dict(os.environ, ROOT=str(root), PXB_IMAGE='fixture:version'), check=True)
            docker = root / 'bin/docker'; docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n'); docker.chmod(0o755)
            result = subprocess.run(['bash', root / 'bin/xtrabackup', '--backup', '--datadir=' + str(root / 'mysql')],
                                    env=dict(os.environ, PATH=str(root / 'bin') + ':' + os.environ['PATH']),
                                    capture_output=True, text=True, check=True)
            self.assertIn('--datadir=' + str(root / 'mysql'), result.stdout.splitlines())
            self.assertNotIn(str(root / 'mysql') + ':/var/lib/mysql', result.stdout.splitlines())
            missing = subprocess.run(['bash', root / 'bin/xtrabackup', '--backup'],
                                     env=dict(os.environ, PATH=str(root / 'bin') + ':' + os.environ['PATH']),
                                     capture_output=True, text=True, check=True)
            self.assertFalse(any(x.startswith('--datadir') for x in missing.stdout.splitlines()))

    def test_actual_lsn_chain_function_rejects_gap_missing_and_reversed_inputs(self):
        source = (ROOT / 'tests/helpers/mybackup-lifecycle.sh').read_text()
        function = 'check_lsn_chain() {' + source.split('check_lsn_chain() {', 1)[1].split('\n}', 1)[0] + '\n}'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [root / name for name in ('full', 'incr1', 'incr2')]
            for p, a, b in zip(files, (0, 100, 200), (100, 200, 300)):
                p.write_text(f'from_lsn = {a}\nto_lsn = {b}\n')
            def execute(paths):
                return subprocess.run(['bash', '-euc', function + '\ncheck_lsn_chain "$@"', 'contract', *map(str, paths)],
                                      capture_output=True, text=True)
            self.assertEqual(execute(files).returncode, 0)
            self.assertNotEqual(execute([files[0], files[2]]).returncode, 0)
            self.assertNotEqual(execute(list(reversed(files))).returncode, 0)
            files[1].write_text('from_lsn = 100\n')
            self.assertNotEqual(execute(files).returncode, 0)

    def test_focused_and_full_handoff_use_exact_native_hosts_and_versions(self):
        self.assertEqual(GATE.HOSTS, {'rocky8': ('root@192.168.47.137','8.10'),
                                    'rocky9': ('root@192.168.122.216','9.8')})
        self.assertEqual(GATE.VERSIONS, ('8.0.33', '8.0.46'))
        source = (ROOT / 'tests/helpers/mysql80-physical-live.py').read_text()
        self.assertLess(source.index('images, runtime = admit(stage)'), source.index('with infrastructure().control_leases():'))
        fixture = (ROOT / 'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        self.assertLess(fixture.index('MYBACKUP_VERSION_MISMATCH'), fixture.index('source "$(dirname'))
        self.assertIn('MYBACKUP_MYSQL_DIGEST', source)
        self.assertIn('MYBACKUP_PXB_DIGEST', source)
        self.assertEqual(len(GATE.scenarios('8.0.33')), 4)
        self.assertEqual(len(GATE.scenarios('full')), 8)
        self.assertEqual(GATE.scenarios('probe'), [('8.0.33', 'host', 'OFF')])
        self.assertEqual({(m, g) for _, m, g in GATE.scenarios('8.0.33')},
                         {('host', 'OFF'), ('host', 'ON'), ('container', 'OFF'), ('container', 'ON')})
        self.assertIn('MYBACKUP_EXECUTION_MODE=mode', source)
        self.assertTrue(all(' mode=' in x for x in GATE.markers('full', 'rocky8') if x.startswith('MYBACKUP_LIFECYCLE_PASS')))

    def test_saved_pass_cannot_replace_current_raw_and_cleanup_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); record = root / 'proof.json'
            record.write_text(json.dumps({'inputs': {}, 'rc': 0, 'hosts': {}}))
            with mock.patch.object(GATE, 'inputs', return_value={}), self.assertRaisesRegex(ValueError, 'MYSQL80_QUALIFICATION_STALE'):
                GATE.verify_record(record)


if __name__ == '__main__':
    unittest.main()
