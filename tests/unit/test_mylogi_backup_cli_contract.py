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


if __name__ == "__main__":
    unittest.main()
