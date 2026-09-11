"""Deterministic contracts for the standalone MySQL migration CLI."""

import importlib.util
import os
import stat
import tempfile
import unittest
import gzip
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/mysqltools/MigrationCli.py"
SPEC = importlib.util.spec_from_file_location("migrationcli", TOOL)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)


class MigrationCliContractTests(unittest.TestCase):
    def test_import_detects_gzip_magic_even_without_gz_suffix(self):
        options = MOD.MigrationOptions(compress_dump=False)
        manager = MOD.MySQLDumpManager(
            MOD.MigrationLogger(), options, MOD.ProcessRegistry()
        )
        path = os.path.join(manager.temp_dir, "dump.sql")
        with gzip.open(path, "wb") as fh:
            fh.write(b"SELECT 1;\n")
        seen = {}
        def fake_run(cmd, timeout, stdin_file=None):
            seen["data"] = stdin_file.read() if stdin_file else b""
            return 0, ""
        manager._run_subprocess = fake_run
        try:
            manager.execute_import(
                MOD.DatabaseConfig("db", 3306, "u", "p", "fixture"), path
            )
            self.assertEqual(seen["data"], b"SELECT 1;\n")
        finally:
            manager.cleanup()

    def test_mysql_admin_connection_does_not_select_system_schema(self):
        connector = MOD.DatabaseConnector(
            MOD.DatabaseConfig("db", 3306, "u", "p", "fixture")
        )
        fake_conn = mock.MagicMock()
        with mock.patch.object(MOD.pymysql, "connect", return_value=fake_conn) as connect:
            with connector.get_connection("mysql"):
                pass
        self.assertIsNone(connect.call_args.kwargs["database"])

    def test_live_runner_pins_mysql_auth_dependency_and_failure_cleanup(self):
        runner = (ROOT / "tests/run_tools_regression.sh").read_text()
        helper = (ROOT / "tests/helpers/migration-live-regression.sh").read_text()
        self.assertIn("PyMySQL==1.1.2 cryptography==44.0.2", runner)
        self.assertIn("/tmp/MigrationCli-tools-live.py", helper)
        self.assertIn("/tmp/migration-live-regression.sh", helper)
        self.assertIn("/tmp/migration-live-report", helper)

    def test_option_file_escapes_special_credentials_and_is_private(self):
        cfg = MOD.DatabaseConfig(
            host="db.example",
            port=3306,
            user='u"ser',
            password='p\\word"with\nnewline',
            database="fixture",
        )
        with MOD.CredentialManager.defaults_extra_file(
            cfg, MOD.MigrationOptions()
        ) as path:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)
            content = Path(path).read_text()
            self.assertIn('user="u\\"ser"', content)
            self.assertIn('password="p\\\\word\\"with\\nnewline"', content)
            self.assertNotIn("p\\word\"with\nnewline", content)
            self.assertNotIn("net_read_timeout", content)
            self.assertNotIn("net_write_timeout", content)
        self.assertFalse(os.path.exists(path))

    def test_dump_path_boundary_does_not_allow_prefix_bypass(self):
        manager = MOD.MySQLDumpManager(
            MOD.MigrationLogger(), MOD.MigrationOptions(), MOD.ProcessRegistry()
        )
        try:
            outside = manager.temp_dir + "-sibling/escape.sql"
            with self.assertRaises(ValueError):
                manager._register_dump_path(outside)
        finally:
            manager.cleanup()

    def test_unknown_mysql_series_is_blocked_until_officially_reviewed(self):
        with self.assertRaises(RuntimeError):
            MOD.classify_release("8.5.0", (8, 5, 0))
        with self.assertRaises(RuntimeError):
            MOD.classify_release("10.0.0", (10, 0, 0))

    def test_8_0_to_9_x_requires_intermediate_8_4_for_in_place_upgrade(self):
        source = MOD.ServerInfo("8.0.46", (8, 0, 46), "OFF", False, 1)
        target = MOD.ServerInfo("9.0.1", (9, 0, 1), "OFF", False, 2)
        profile = MOD.MySQLCompatibilityEngine.analyze(source, target)
        self.assertFalse(profile.in_place_officially_supported)
        self.assertTrue(any("8.4" in warning for warning in profile.warnings))

    def test_older_innovation_to_8_4_is_logical_only(self):
        source = MOD.ServerInfo("8.1.0", (8, 1, 0), "OFF", False, 1)
        target = MOD.ServerInfo("8.4.4", (8, 4, 4), "OFF", False, 2)
        profile = MOD.MySQLCompatibilityEngine.analyze(source, target)
        self.assertFalse(profile.in_place_officially_supported)
        self.assertTrue(any("逻辑" in warning for warning in profile.warnings))

    def test_checksum_contract_uses_extended_checksum_and_reports_match(self):
        class Cursor:
            def execute(self, sql):
                self.sql = sql

            def fetchone(self):
                return ("fixture", 1234)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def cursor(self):
                return Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        connector = MOD.DatabaseConnector(
            MOD.DatabaseConfig("db", 3306, "u", "p", "fixture")
        )
        with mock.patch.object(connector, "get_connection", return_value=Connection()):
            self.assertEqual(connector.get_table_checksum("items"), 1234)

    def test_cross_version_checksum_mismatch_is_audited_but_not_failure(self):
        source = mock.Mock()
        target = mock.Mock()
        source.get_tables.return_value = [MOD.TableInfo("items", "INNODB", 1, 1)]
        target.get_tables.return_value = [MOD.TableInfo("items", "INNODB", 1, 1)]
        source.get_table_row_count.return_value = 1
        target.get_table_row_count.return_value = 1
        source.get_table_checksum.return_value = 11
        target.get_table_checksum.return_value = 22
        options = MOD.MigrationOptions(table_checksum=True)
        report = MOD.MigrationValidator(options).validate(
            source, target, MOD.MigrationMode.STRUCTURE_AND_DATA, checksum_strict=False
        )
        self.assertTrue(report["passed"])
        self.assertFalse(report["checksum_comparable"])
        self.assertFalse(report["tables"][0]["checksum_match"])

    def test_task_options_are_used_by_post_migration_validator(self):
        manager = MOD.MigrationManager(
            MOD.MigrationOptions(), MOD.ProcessRegistry(), MOD.HostConcurrencyLimiter()
        )
        task_options = MOD.MigrationOptions(table_checksum=True)
        task = MOD.MigrationTask(
            MOD.DatabaseConfig("source", 3306, "u", "p", "db"),
            MOD.DatabaseConfig("target", 3306, "u", "p", "db2"),
            task_options,
        )
        source = mock.Mock()
        target = mock.Mock()
        with mock.patch.object(MOD.DatabaseConnector, "get_server_info") as info:
            info.side_effect = [
                MOD.ServerInfo("8.0.46", (8, 0, 46), "OFF", False, 1),
                MOD.ServerInfo("8.4.4", (8, 4, 4), "OFF", False, 2),
            ]
            with mock.patch.object(manager, "_migrate_per_table"):
                with mock.patch.object(manager, "_migrate_whole_database"):
                    with mock.patch.object(MOD.DatabaseConnector, "get_tables", return_value=[]):
                        with mock.patch.object(MOD.DatabaseConnector, "estimate_database_bytes", return_value=0):
                            with mock.patch.object(MOD.PreflightChecker, "check_target_empty"):
                                fake_validator = mock.Mock()
                                fake_validator.validate.return_value = {"passed": True}
                                with mock.patch.object(
                                    MOD, "MigrationValidator", return_value=fake_validator
                                ) as validator_ctor:
                                    with mock.patch.object(
                                        MOD.subprocess, "check_output", return_value="mysqldump  Ver 8.4.4"
                                    ):
                                        manager.execute_migration(task)
        self.assertTrue(validator_ctor.call_args.args[0].table_checksum)
        self.assertEqual(fake_validator.validate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
