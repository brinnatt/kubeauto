"""Deterministic contracts for the standalone MySQL migration CLI."""

import importlib.util
import os
import stat
import tempfile
import unittest
import gzip
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/mysqltools/MigrationCli.py"
SPEC = importlib.util.spec_from_file_location("migrationcli", TOOL)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)


class MigrationCliContractTests(unittest.TestCase):
    def test_full_launcher_refuses_pending_prerequisites_before_remote_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            matrix = directory / "matrix.yaml"
            text = (ROOT / "tests/tools-test-matrix.yaml").read_text()
            import yaml
            data = yaml.safe_load(text)
            for case in data['test_cases']:
                if case['id'] == 'TL-MIG-01':
                    case['status'] = 'pending'
            # Validator needs accurate summary counts, independent of current acceptance.
            from collections import Counter
            counts = Counter(case['status'] for case in data['test_cases'])
            data['coverage_summary'].update({key: counts[key] for key in ('pass','pending','fail','skip','na')})
            matrix.write_text(yaml.safe_dump(data, allow_unicode=True))
            launcher = directory / "runner.sh"
            text = (ROOT / "tests/run_tools_regression.sh").read_text()
            text = text.replace('ROOT="$(cd "$(dirname "$0")/.." && pwd)"', 'ROOT='+str(ROOT))
            text = text.replace('MATRIX="$ROOT/tests/tools-test-matrix.yaml"', 'MATRIX='+str(matrix))
            # The inline final gate reads from ROOT, so inject only the matrix fixture path.
            text = text.replace("(root/'tests/tools-test-matrix.yaml').read_text()", "pathlib.Path("+repr(str(matrix))+").read_text()")
            launcher.write_text(text)
            env = dict(os.environ, MIGRATION_STAGE="full", MIGRATION_TEST_HOST="ubuntu@192.168.47.138")
            result = subprocess.run(["bash", str(launcher), "--migration-live"], env=env, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MIGRATION_FINAL_PREREQUISITES_REFUSED", result.stderr)

    def test_fixture_refuses_foreign_containers_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # Rewrite fixed lock/fixture roots only; run the real shell control flow.
            helper = (ROOT / "tests/helpers/migration-live-regression.sh").read_text()
            helper = helper.replace("/run/lock/", str(directory)+"/")
            script = directory / "helper.sh"; script.write_text(helper)
            docker = directory / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\nif [ \"$1\" = ps ]; then echo tools-mig-src; fi\n")
            docker.chmod(0o755)
            calls = directory / "calls"
            env = dict(os.environ, PATH=str(directory)+":"+os.environ['PATH'], CALLS=str(calls), MIGRATION_TOOL="/fixture/MigrationCli")
            result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls.read_text().strip(), "ps -a --format {{.Names}}")

    def test_selected_build_keeps_backup_branch_and_migration_provenance_separate(self):
        runner = (ROOT / "tests/run_tools_regression.sh").read_text()
        helper = (ROOT / "tests/helpers/build-tools-rocky8.sh").read_text()
        self.assertIn('"$TOOLS_SELECTED" == MigrationCli', runner)
        self.assertIn('"$TOOLS_SELECTED" == MigrationCli', helper)
        self.assertIn("dist/mysql-migration-build.json", runner)
        self.assertIn("selected == {'MigrationCli'}", runner)
        self.assertIn("PyMySQL==1.1.2 cryptography==45.0.5", helper)
        # Guard checks happen before a remote build can run.
        for value in ("MigrationCli,StarCli", "StarCli", "MigrationCli,MigrationCli"):
            env = dict(os.environ, TOOLS_SELECTED=value)
            result = subprocess.run(["bash", str(ROOT / "tests/run_tools_regression.sh"), "--build-only"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("TOOLS_SELECTED_SCOPE_REFUSED", result.stderr)

    def test_native_command_builders_preserve_argv_and_positionals(self):
        options = MOD.MigrationOptions(native_options={
            "mysqldump": ["--net-buffer-length=32768"],
            "mysql": ["--connect-timeout=45"],
        })
        manager = MOD.MySQLDumpManager(MOD.MigrationLogger(), options, MOD.ProcessRegistry())
        cfg = MOD.DatabaseConfig("db", 3306, "u", "p", "fixture")
        try:
            dump = manager.build_mysqldump_command(
                cfg, "/tmp/private.cnf", "/tmp/dump.sql", ["items"], False, False, True,
                "--ssl-ca=/tmp/source CA.pem", max_allowed_packet="256M")
            self.assertEqual(dump[1], "--defaults-extra-file=/tmp/private.cnf")
            for token in ("--net-buffer-length=32768", "--ssl-ca=/tmp/source CA.pem", "--max-allowed-packet=256M"):
                self.assertLess(dump.index(token), dump.index("fixture"))
            self.assertEqual(dump[-3:], ["fixture", "items", "--result-file=/tmp/dump.sql"])
            client = manager.build_mysql_command(cfg, "/tmp/private.cnf", "--ssl-ca=/tmp/target CA.pem", connect_timeout=50)
            self.assertEqual(client[-1], "fixture")
            self.assertLess(client.index("--connect-timeout=45"), client.index("--connect-timeout=50"))
        finally:
            manager.cleanup()

    def test_native_config_inheritance_override_clear_and_cli_append(self):
        global_opts = {"native_options": {"mysql": ["--connect-timeout=45"], "mysqldump": ["--net-buffer-length=32768"]}}
        task_opts = {"native_options": {"mysqldump": []}}
        merged = MOD.merge_options_dict(global_opts, task_opts)
        with mock.patch.object(sys, "argv", ["MigrationCli", "--native-option", "mysql=--connect-timeout=50"]):
            options = MOD.build_migration_options(MOD.parse_arguments(), merged)
        self.assertEqual(options.native_options, {"mysql": ["--connect-timeout=45", "--connect-timeout=50"], "mysqldump": []})
        options.native_options["mysql"].append("--connect-timeout=60")
        self.assertEqual(global_opts["native_options"]["mysql"], ["--connect-timeout=45"])
        self.assertEqual(task_opts["native_options"]["mysqldump"], [])

    def test_native_protected_aliases_and_bad_config_are_rejected(self):
        for target in MOD.NATIVE_TARGETS:
            for option in ("--pass=x", "--loose_skip_force", "--result_file=/tmp/x", "--databases", "--skip-routines", "--ignore-error=1146", "--column-statistics=1", "--init-command=DROP DATABASE app", "--encryption-key=x", "--no-data", "--skip-lock-tables"):
                with self.subTest(target=target, option=option), self.assertRaises(ValueError):
                    MOD.native_arguments(target, option)
        for value in ([], None, {"mysql": "--connect-timeout=45"}, {"mysql": [1]}, {"unknown": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MOD.validate_native_config(value)
        self.assertEqual(MOD.native_arguments("mysqldump", net_buffer_length=[16384,32768], extended_insert=False), ["--net-buffer-length=16384", "--net-buffer-length=32768", "--extended-insert=OFF"])

    def test_native_help_and_rejection_have_no_runtime_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            for arguments, expected in [(["--help-native"], 0), (["--help"], 0), (["--guide"], 0), (["--native-option", "mysql=--force"], 2)]:
                result = subprocess.run([sys.executable, str(TOOL), *arguments], cwd=directory, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertNotIn("pymysql.err", result.stderr)
            self.assertIn("--help-native", subprocess.check_output([sys.executable, str(TOOL), "--help"], text=True))

    def test_concurrent_task_options_and_compatibility_are_isolated(self):
        manager = MOD.MySQLDumpManager(MOD.MigrationLogger(), MOD.MigrationOptions(), MOD.ProcessRegistry())
        barrier = threading.Barrier(2)
        def worker(number):
            manager.options = MOD.MigrationOptions(native_options={"mysql": [f"--connect-timeout={number}"]})
            manager.set_dump_context(str(number), bool(number % 2))
            context = MOD.DumpContext()
            context.extra_mysql_args = [f"--max-allowed-packet={number}M"]
            manager.set_path_context(context)
            barrier.wait(timeout=5)
            cfg = MOD.DatabaseConfig("db", 3306, "u", "p", "fixture")
            return number, manager._gtid_purged, manager._skip_lock_tables, manager.build_mysql_command(cfg, "/tmp/p.cnf")
        try:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(worker, [10, 21]))
            for number, gtid, skip_lock, command in results:
                self.assertEqual(gtid, str(number))
                self.assertEqual(skip_lock, bool(number % 2))
                self.assertIn(f"--connect-timeout={number}", command)
                self.assertIn(f"--max-allowed-packet={number}M", command)
            self.assertEqual(manager.options.native_options, {})
        finally:
            manager.cleanup()

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

    def test_live_runner_uses_fixture_container_mysql_clients(self):
        helper = (ROOT / "tests/helpers/migration-live-regression.sh").read_text()
        runner = (ROOT / "tests/run_tools_regression.sh").read_text()
        for name in ("tools-mig-client80", "tools-mig-client84", "tools-mig-client92"):
            self.assertIn(name, helper)
        self.assertIn("-P13306) container=\"$CLIENT80\"", helper)
        self.assertIn("-P13307) container=\"$CLIENT84\"", helper)
        self.assertIn("-P13308) container=\"$CLIENT92\"", helper)
        self.assertIn(
            'exec docker exec -i "$container" mysql "$@"', helper
        )
        self.assertIn(
            'exec docker exec -i "$container" mysqldump "$@"', helper
        )
        self.assertNotRegex(helper, r"(?m)^\s*mysql\s")

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
