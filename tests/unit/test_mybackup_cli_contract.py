import importlib.util
import json
import os
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "mybackupcli", ROOT / "tools/mysqltools/MyBackupCli.py"
)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)


def manager_for(tmpdir):
    config = mock.Mock()
    config.backup_base = Path(tmpdir) / "backup"
    config.mysql_datadir = Path(tmpdir) / "mysql"
    config.mysql_binlog_prefix = "mysql-bin"
    config.lock_file = config.backup_base / "lock/mysqlbackup.lock"
    config.audit_base = config.backup_base / "restore_audit"
    manager = MOD.BackupManager.__new__(MOD.BackupManager)
    manager.config = config
    manager.today = "2026-09-11"
    manager.now = "120000"
    manager.logger = logging.getLogger("mybackup-contract")
    return manager


class MyBackupCliContractTest(unittest.TestCase):
    def test_binlog_index_absolute_entries_are_normalized_to_datadir_names(self):
        manager = MOD.BackupManager.__new__(MOD.BackupManager)
        manager.config = mock.Mock()
        manager.config.mysql_datadir = Path("/var/lib/mysql")
        manager.config.mysql_binlog_prefix = "mysql-bin"
        with tempfile.TemporaryDirectory() as td:
            datadir = Path(td) / "mysql"
            datadir.mkdir()
            index = datadir / "mysql-bin.index"
            index.write_text("/var/lib/mysql/mysql-bin.000001\n./mysql-bin.000002\n")
            self.assertEqual(
                [Path(line.strip()).name for line in index.read_text().splitlines()],
                ["mysql-bin.000001", "mysql-bin.000002"],
            )

    def test_failed_external_command_stderr_is_visible(self):
        manager = MOD.BackupManager.__new__(MOD.BackupManager)
        manager.config = mock.Mock()
        manager.logger = mock.Mock()
        proc = mock.Mock(returncode=1, stdout="", stderr="denied")
        with mock.patch.object(MOD.subprocess, "run", return_value=proc), mock.patch("sys.stderr") as err:
            with self.assertRaises(RuntimeError):
                manager.run_cmd(["xtrabackup", "--password=secret"])
        err.write.assert_called()
        manager.logger.error.assert_called_once_with("denied")

    def test_config_rejects_group_or_world_readable_password_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "backup.json"
            cfg.write_text(json.dumps({"mysql_password": "secret"}))
            cfg.chmod(0o644)
            with self.assertRaises(SystemExit):
                MOD.Config(str(cfg))

    def test_config_rejects_non_positive_xtrabackup_threads(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "backup.json"
            cfg.write_text(json.dumps({
                "mysql_password": "secret", "xtrabackup_parallel": 0
            }))
            cfg.chmod(0o600)
            with mock.patch.object(MOD.Config, "_detect_mysql_version", return_value=(8, 4)):
                with self.assertRaises(SystemExit):
                    MOD.Config(str(cfg))

    def test_config_rejects_mysql_nine_for_physical_backup(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "backup.json"
            cfg.write_text(json.dumps({"mysql_password": "secret"}))
            cfg.chmod(0o600)
            with mock.patch.object(MOD.Config, "_detect_mysql_version", return_value=(9, 0)):
                with self.assertRaises(SystemExit):
                    MOD.Config(str(cfg))

    def test_command_redacts_inline_password(self):
        self.assertNotIn(
            "s3cret",
            MOD.BackupManager._redact_command(
                ["mysql", "--password=s3cret", "--socket=/run/mysql.sock"]
            ),
        )
        self.assertIn("--password=***", MOD.BackupManager._redact_command(["--password=s3cret"]))

    def test_lock_conflict_is_nonzero_failure(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            mgr.ensure_dirs()
            first = mgr.acquire_lock()
            try:
                with self.assertRaises(SystemExit) as raised:
                    mgr.acquire_lock()
                self.assertEqual(raised.exception.code, 1)
            finally:
                MOD.fcntl.flock(first, MOD.fcntl.LOCK_UN)
                os.close(first)

    def test_latest_full_date_ignores_symlink_and_non_date_directories(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            full = mgr.config.backup_base / "full"
            (full / "2026-09-10").mkdir(parents=True)
            (full / "not-a-date").mkdir()
            (full / "latest_raw").symlink_to(full / "2026-09-10")
            self.assertEqual(mgr.latest_full_date(), "2026-09-10")

    def test_incremental_refuses_unmarked_latest_full(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            full = mgr.config.backup_base / "full/2026-09-11/120000/backup"
            full.mkdir(parents=True)
            (mgr.config.backup_base / "full/latest_raw").symlink_to(full)
            with self.assertRaises(SystemExit):
                mgr.incr_backup()

    def test_binlog_archive_does_not_advance_past_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            mgr.config.mysql_datadir.mkdir(parents=True)
            (mgr.config.mysql_datadir / "mysql-bin.000001").write_bytes(b"one")
            (mgr.config.mysql_datadir / "mysql-bin.000001.index").write_text("")
            (mgr.config.mysql_datadir / "mysql-bin.index").write_text(
                "./mysql-bin.000001\n./mysql-bin.000002\n"
            )
            mgr.ensure_dirs()
            mgr.binlog_backup(backall=True)
            state = mgr.config.backup_base / "binlog/state/last_archived"
            self.assertEqual(state.read_text(), "mysql-bin.000001")
            self.assertTrue((mgr.config.backup_base / "binlog/2026-09-11/mysql-bin.000001").exists())

    def test_binlog_sequence_is_numeric_and_rejects_gap(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            directory = Path(td) / "binlog"
            directory.mkdir()
            for name in ("mysql-bin.000010", "mysql-bin.000009"):
                (directory / name).write_bytes(b"x")
            self.assertEqual(
                mgr.check_binlog_sequence(directory),
                ["mysql-bin.000009", "mysql-bin.000010"],
            )
            (directory / "mysql-bin.000012").write_bytes(b"x")
            with self.assertRaises(SystemExit):
                mgr.check_binlog_sequence(directory)

    def test_restore_requires_explicit_destructive_confirmation(self):
        mgr = manager_for(tempfile.mkdtemp())
        with self.assertRaises(SystemExit):
            mgr.restore(date="2026-09-11", binlog_start_time="2026-09-11 00:00:00")

    def test_restore_restarts_active_service_after_interrupt(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            plan_file = Path(td) / "restore.plan.json"
            service_calls = []

            def service(action, timeout=30):
                service_calls.append((action, timeout))
                # status before restore, stop, status during recovery, start
                return [True, True, False, True][len(service_calls) - 1]

            with mock.patch.object(
                mgr, "_load_restore_plan", return_value=({"binlog": []}, plan_file)
            ), mock.patch.object(mgr, "manage_mysql_service", side_effect=service), mock.patch.object(
                mgr, "binlog_backup"
            ), mock.patch.object(
                mgr, "_decompress_backup", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    mgr.restore(
                        date="2026-09-11",
                        binlog_start_time="2026-09-11 00:00:00",
                        confirm_destructive=True,
                    )

            self.assertIn(("start", 60), service_calls)

    def test_restore_plan_rejects_path_outside_backup_base(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            plan_file = mgr.config.audit_base / "2026-09-11/restore.plan.json"
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(
                '{"date":"2026-09-11","status":"READY",'
                '"full":"/tmp/outside-backup","incr":[]}'
            )
            with self.assertRaises(SystemExit):
                mgr._load_restore_plan("2026-09-11")

    def test_restore_plan_rejects_missing_incremental_directory(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            full = mgr.config.backup_base / "full/2026-09-11/120000/backup"
            full.mkdir(parents=True)
            missing = mgr.config.backup_base / "incr/missing"
            plan_file = mgr.config.audit_base / "2026-09-11/restore.plan.json"
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(json.dumps({
                "date": "2026-09-11", "status": "READY",
                "full": str(full), "incr": [str(missing)]
            }))
            with self.assertRaises(SystemExit):
                mgr._load_restore_plan("2026-09-11")

    def test_restore_plan_rejects_binlog_path_like_name(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            full = mgr.config.backup_base / "full/2026-09-11/120000/backup"
            full.mkdir(parents=True)
            plan_file = mgr.config.audit_base / "2026-09-11/restore.plan.json"
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(json.dumps({
                "date": "2026-09-11", "status": "READY",
                "full": str(full), "incr": [], "binlog": ["../secret"]
            }))
            with self.assertRaises(SystemExit):
                mgr._load_restore_plan("2026-09-11")

    def test_binlog_apply_failure_is_propagated(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            binlog_dir = mgr.config.backup_base / "binlog"
            binlog_dir.mkdir(parents=True)
            (binlog_dir / "mysql-bin.000001").write_bytes(b"x")
            proc1 = mock.Mock(returncode=3, stderr=mock.Mock(read=lambda: b"decoder failed"))
            proc2 = mock.Mock(returncode=0)
            proc2.communicate.return_value = (b"", b"")
            with mock.patch.object(MOD.subprocess, "Popen", side_effect=[proc1, proc2]):
                with self.assertRaises(RuntimeError):
                    mgr._apply_binlog(
                        {"binlog": ["mysql-bin.000001"]},
                        "2026-09-11 00:00:00",
                        None,
                    )

    def test_incremental_prepare_follows_official_apply_log_only_order(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            calls = []
            mgr.run_cmd = lambda cmd, check=True: calls.append(cmd)
            mgr._prepare_backup({
                "full": "/backup/full",
                "incr": ["/backup/inc1", "/backup/inc2"],
            })
            self.assertIn("--apply-log-only", calls[0])
            self.assertIn("--apply-log-only", calls[1])
            self.assertNotIn("--apply-log-only", calls[2])
            self.assertEqual(calls[1][calls[1].index("--incremental-dir") + 1], "/backup/inc1")
            self.assertEqual(calls[2][calls[2].index("--incremental-dir") + 1], "/backup/inc2")


if __name__ == "__main__":
    unittest.main()
