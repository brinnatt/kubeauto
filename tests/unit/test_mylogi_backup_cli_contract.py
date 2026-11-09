"""Deterministic contracts for the standalone MyLogiBackupCli."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "mysqltools" / "MyLogiBackupCli.py"


def load_module():
    spec = importlib.util.spec_from_file_location("mylogi_backup_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MyLogiBackupCliContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_ignore_error_cannot_publish_a_partial_dump(self):
        for target in self.module.NATIVE_TARGETS:
            for token in ("--ignore-error=1146", "--loose-ignore_error=1146", "--ignore-err=1146"):
                with self.subTest(target=target, token=token), self.assertRaises(ValueError):
                    self.module.native_arguments(target, token)

    def test_help_is_complete_and_excludes_retired_formats_and_versions(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for marker in (
            "setup-login", "preflight", "backup", "timer install", "timer status",
            "timer remove", "--database", "--exclude-database", "--compression",
            "MYSQL_BACKUP_OK", "MYLOGI_MYSQL_CONFIG_EDITOR",
        ):
            self.assertIn(marker, result.stdout)
        lowered = result.stdout.lower()
        self.assertNotIn("mysql 5.7", lowered)
        self.assertNotIn("master-data", lowered)
        self.assertNotIn("zip64", lowered)
        self.assertNotIn(".zip", lowered)

    def test_customer_help_is_available_from_each_native_parameter_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'never.log'
            for prefix in ([], ['backup'], ['preflight'], ['timer', 'install']):
                for option in ('--help', '--help-native'):
                    with self.subTest(prefix=prefix, option=option):
                        result = subprocess.run([sys.executable, str(SCRIPT)] + prefix + [option],
                            env=dict(os.environ, PATH='/nonexistent', MYLOGI_LOG_FILE=str(log)),
                            capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, '')
                        self.assertIn('--native-option', result.stdout)
                        self.assertIn('mysqldump', result.stdout)
                        self.assertFalse(log.exists())
            for protected in self.module.NATIVE_PROTECTED:
                self.assertIn('--' + protected, result.stdout)
            self.assertIn('正常入口', result.stdout)

    def test_native_help_examples_are_accepted_and_keep_quoted_values(self):
        import shlex
        for line in self.module.native_option_help().splitlines():
            if line.strip().startswith('MyLogiBackupCli '):
                with self.subTest(example=line):
                    args = self.module.create_parser().parse_args(shlex.split(line)[1:])
                    self.module.parse_native_options(getattr(args, 'native_option', []))
                    if 'backup CA.pem' in line:
                        self.assertEqual(args.native_option, ['mysqldump=--ssl-ca=/etc/mysql/backup CA.pem'])

    def test_version_command(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--version"], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "MyLogiBackupCli 1.0.0")

    def test_dump_contract_is_mysql_8_source_data_only(self):
        selection = {"databases": ["app", "audit"]}
        with mock.patch.dict(os.environ, {"MYLOGI_MYSQLDUMP_BIN": "/opt/mysql/bin/mysqldump"}):
            command = self.module.build_dump_command(
                selection, Path("/backups/.dump.sql.partial"), "mysql-backup"
            )
        self.assertIn("--source-data=2", command)
        self.assertNotIn("--master-data=2", command)
        self.assertIn("--single-transaction", command)
        self.assertIn("--routines", command)
        self.assertIn("--events", command)
        self.assertIn("--triggers", command)
        self.assertIn("--hex-blob", command)
        self.assertEqual(command[-3:], ["--databases", "app", "audit"])

    def test_versions_before_mysql_8_are_rejected(self):
        with self.assertRaisesRegex(self.module.BackupError, "仅支持 MySQL 8.0"):
            self.module.require_mysql_8_or_newer((7, 9, 99), "服务端")
        self.module.require_mysql_8_or_newer((8, 0, 46), "服务端")
        self.module.require_mysql_8_or_newer((9, 2, 0), "服务端")

    def test_database_selection_scopes_and_rejects_duplicates(self):
        args = SimpleNamespace(
            database=[["app", "audit"]], exclude_database=[["audit"]],
            login_path="mysql-backup", compression="xz",
        )
        with mock.patch.object(
            self.module, "mysql_query",
            return_value="information_schema\nmysql\napp\naudit\nsys\n",
        ):
            selection = self.module.resolve_selection(args)
        self.assertEqual(selection["databases"], ["app"])
        self.assertTrue(selection["scope"].startswith("selected-"))
        self.assertEqual(selection["compression"], "xz")
        args.database = [["app", "app"]]
        with self.assertRaisesRegex(self.module.BackupError, "重复数据库"):
            self.module.resolve_selection(args)

    def test_database_names_allow_mysql_identifiers_but_reject_option_and_control_values(self):
        self.module.validate_database_names(["customer data", "tenant.prod", "订单"], "--database")
        for invalid in ("", "-defaults-file", "bad\nname", "bad\x00name"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                self.module.BackupError, "非法数据库名"
            ):
                self.module.validate_database_names([invalid], "--database")

    def test_system_database_cannot_be_selected(self):
        args = SimpleNamespace(
            database=[["information_schema"]], exclude_database=[],
            login_path="mysql-backup", compression="gz",
        )
        with mock.patch.object(self.module, "mysql_query", return_value="information_schema\napp\n"):
            with self.assertRaisesRegex(self.module.BackupError, "系统数据库"):
                self.module.resolve_selection(args)

    def test_each_archive_format_round_trips_sql_and_checksum(self):
        for compression in ("gz", "bz2", "xz"):
            with self.subTest(compression=compression), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sql = root / "backup.sql"
                sql.write_bytes(b"CREATE DATABASE app;\n-- Dump completed on 2026-09-23\n")
                digest = self.module.file_sha256(sql)
                archive = root / ("backup.tar." + compression)
                self.module.write_archive(sql, archive, sql.name, digest, compression)
                with tarfile.open(archive, "r:*") as source:
                    self.assertEqual(source.getnames(), ["backup.sql", "backup.sql.sha256"])
                    self.assertEqual(source.extractfile("backup.sql").read(), sql.read_bytes())

    def test_corrupt_dump_footer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.sql"
            path.write_text("SELECT 1;\n", encoding="utf-8")
            with self.assertRaisesRegex(self.module.BackupError, "完成标记"):
                self.module.validate_footer(path)

    def test_retention_is_scoped_to_three_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for index in range(5):
                path = root / "mysql-selected-deadbeef0000-20260923T00000{}Z.tar.gz".format(index)
                path.write_bytes(b"archive")
                timestamp = time.time() + index
                os.utime(path, (timestamp, timestamp))
                files.append(path)
            unrelated = root / "mysql-all-20260923T000000Z.tar.gz"
            unrelated.write_bytes(b"archive")
            self.module.enforce_retention(root, "selected-deadbeef0000")
            self.assertEqual(len(list(root.glob("mysql-selected-deadbeef0000-*.tar.gz"))), 3)
            self.assertTrue(unrelated.exists())

    def test_preflight_does_not_remove_an_active_backup_partial(self):
        source = SCRIPT.read_text(encoding="utf-8")
        preflight_body = source[source.index("def preflight("):source.index("def handle_signal(")]
        backup_body = source[source.index("def backup("):source.index("def setup_login(")]
        self.assertNotIn("remove_partials", preflight_body)
        self.assertLess(backup_body.index("fcntl.flock"), backup_body.index("remove_partials"))
        self.assertLess(
            backup_body.index("fcntl.flock"), backup_body.index("MYSQL_BACKUP_LOCK_ACQUIRED")
        )
        self.assertLess(
            backup_body.index("MYSQL_BACKUP_LOCK_ACQUIRED"), backup_body.index("subprocess.Popen")
        )

    def test_command_environment_restores_frozen_linker_boundary(self):
        with mock.patch.object(self.module.sys, "frozen", True, create=True), mock.patch.dict(
            os.environ,
            {"LD_LIBRARY_PATH": "/tmp/pyinstaller", "LD_LIBRARY_PATH_ORIG": "/usr/lib64"},
            clear=False,
        ):
            environment = self.module._env_for_system_subprocess()
        self.assertEqual(environment["LD_LIBRARY_PATH"], "/usr/lib64")

    def test_timer_command_preserves_backup_scope(self):
        args = SimpleNamespace(
            login_path="mysql-backup", compression="bz2",
            database=[["app", "audit"]], exclude_database=[["audit"]],
        )
        command = self.module.timer_backup_arguments(args)
        self.assertIn("backup --login-path mysql-backup --compression bz2", command)
        self.assertIn("--database app audit", command)
        self.assertIn("--exclude-database audit", command)

    def test_setup_login_never_places_password_value_in_argv(self):
        args = SimpleNamespace(
            host="192.168.47.10", port=3306, user="mysql_backup", login_path="mysql-backup"
        )
        with tempfile.TemporaryDirectory() as directory:
            login_file = Path(directory) / ".mylogin.cnf"
            login_file.write_bytes(b"encrypted")
            captured = []

            def fake_run(command, **_kwargs):
                captured.extend(command)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.dict(os.environ, {"MYLOGI_LOGIN_FILE": str(login_file)}), \
                    mock.patch.object(self.module, "require_root"), \
                    mock.patch.object(self.module, "require_executable"), \
                    mock.patch.object(self.module, "run_command", side_effect=fake_run), \
                    mock.patch.object(self.module.os, "chown"):
                self.module.setup_login(args)
            self.assertIn("--password", captured)
            self.assertFalse(any(item.startswith("--password=") for item in captured))
            self.assertEqual(login_file.stat().st_mode & 0o777, 0o600)

    def test_setup_login_reports_invalid_host_without_traceback(self):
        args = SimpleNamespace(
            host="mysql.example.com", port=3306, user="mysql_backup", login_path="mysql-backup"
        )
        with mock.patch.object(self.module, "require_root"), \
                mock.patch.object(self.module, "require_executable"):
            with self.assertRaisesRegex(self.module.BackupError, "有效 IPv4"):
                self.module.setup_login(args)

    def test_parser_rejects_unknown_or_ambiguous_arguments(self):
        parser = self.module.create_parser()
        with self.assertRaises(self.module.BackupError):
            parser.parse_args(["backup", "--comp", "gz"])
        with self.assertRaises(self.module.BackupError):
            parser.parse_args(["timer", "install", "--at", "25:99"])


class NativeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_native_options_reach_dump_before_database_names(self):
        command = self.module.build_dump_command({'databases': ['app']}, Path('/tmp/a.sql'), 'backup',
                                                 '--extended-insert', net_buffer_length=16384)
        self.assertIn('--verbose', command)
        self.assertLess(command.index('--extended-insert'), command.index('--databases'))
        self.assertIn('--net-buffer-length=16384', command)
        # Shell punctuation in a value is a single argv element, not executable text.
        self.assertEqual(self.module.native_arguments('mysql', ssl_ca='/tmp/ca $(touch x);.pem'),
                         ['--ssl-ca=/tmp/ca $(touch x);.pem'])

    def test_native_protection_refuses_macro_and_abbreviated_overrides(self):
        for token in ('--result=/tmp/x', '--skip-comments', '--loose_disable_single_transaction',
                      '--force', '--compact', '--skip-opt', '--no-data', '--where=id>0',
                      '--login-path=other', '--password=secret', '--init-command=DROP TABLE t', '--source-data=0'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.module.native_arguments('mysqldump', token)

    def test_invalid_native_cli_fails_before_logs_or_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'never.log'
            env = dict(os.environ, MYLOGI_LOG_FILE=str(log))
            result = subprocess.run([sys.executable, str(SCRIPT), 'backup', '--native-option', 'mysqldump=--force'],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())
            self.assertNotIn('Traceback', result.stderr)

    def test_archive_hashes_sql_only_once_and_preserves_full_verification(self):
        for compression in ('gz', 'bz2', 'xz'):
            with self.subTest(compression=compression), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'data.sql'
                path.write_bytes(b'INSERT INTO t VALUES(1);\n' * 10000)
                target = Path(directory)/('data.tar.'+compression)
                with mock.patch.object(self.module, 'file_sha256', side_effect=AssertionError('extra pass')):
                    digest = self.module.write_archive(path, target, path.name, None, compression)
                self.assertEqual(digest, self.module.hashlib.sha256(path.read_bytes()).hexdigest())
                with tarfile.open(target) as archive:
                    self.assertEqual(archive.extractfile(path.name).read(), path.read_bytes())
                    self.assertEqual(archive.extractfile(path.name+'.sha256').read().decode(), digest+'  '+path.name+'\n')

    def test_archive_readback_detects_changed_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'data.sql'; path.write_bytes(b'original')
            with self.assertRaisesRegex(self.module.BackupError, '压缩时变化'):
                self.module.write_archive(path, Path(directory)/'a.tar.gz', path.name, '0'*64, 'gz')

    def test_previous_size_reads_first_header_without_scanning_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root/'data.sql'; path.write_bytes(b'x'*1024)
            self.module.write_archive(path, root/'mysql-all-20261108.tar.gz', path.name, None, 'gz')
            with mock.patch.object(tarfile.TarFile, 'getmembers', side_effect=AssertionError('full scan')):
                self.assertEqual(self.module.previous_sql_size(root, 'all'), 1024)

    def test_timer_keeps_native_argv_and_escapes_systemd_expansion(self):
        args = SimpleNamespace(login_path='backup', compression='gz', database=[['app']], exclude_database=[],
                               native_option=['mysqldump=--extended-insert', 'mysql=--ssl-ca=/ca/$test%a.pem'])
        import shlex
        tokens = shlex.split(self.module.timer_backup_arguments(args))
        self.assertIn('mysqldump=--extended-insert', tokens)
        self.assertEqual(self.module.systemd_quote('$test%a'), '"$$test%%a"')

    def test_full_compression_trailer_is_verified_not_only_sql_digest(self):
        for damage in ('crc', 'truncated'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); sql=root/'data.sql'; sql.write_bytes(b'INSERT INTO t VALUES(1);\n'*1000)
                archive=root/'data.tar.gz'; original=self.module.tarfile.open
                def open_archive(*args, **kwargs):
                    if kwargs.get('mode') == 'r|':
                        data=bytearray(archive.read_bytes())
                        if damage=='crc': data[-8] ^= 1
                        else: data=data[:-4]
                        archive.write_bytes(data)
                    return original(*args, **kwargs)
                with mock.patch.object(self.module.tarfile, 'open', side_effect=open_archive):
                    with self.assertRaises((OSError, EOFError)):
                        self.module.write_archive(sql, archive, sql.name, None, 'gz')


if __name__ == "__main__":
    unittest.main()
