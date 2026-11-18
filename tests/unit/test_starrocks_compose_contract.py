"""Focused contracts for the two Compose routes; never runs StarCli."""
import copy
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("starrocks_compose_gate", ROOT / "tests/helpers/starrocks-compose-live.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PRODUCT = ROOT / gate.PRODUCT


class ComposeGateContracts(unittest.TestCase):
    def test_sql_failure_preserves_error_and_redacts_secrets(self):
        worker = gate.Worker({"id": "a" * 32, "base": "/tmp/fixture"})
        worker.mode = "cn"
        worker.password = "fixture-password"
        error = subprocess.CalledProcessError(1, ["mysql"], stderr="ERROR 1045: fixture-password access denied")
        worker.compose = Mock(side_effect=error)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), self.assertRaises(subprocess.CalledProcessError) as raised:
            worker.sql("SELECT 1;")
        self.assertIs(raised.exception, error)
        self.assertIn("ERROR 1045", captured.getvalue())
        self.assertNotIn(worker.password, captured.getvalue())
        worker.compose.assert_called_once()

    def test_failure_log_archive_contains_only_component_logs(self):
        import tarfile
        with tempfile.TemporaryDirectory() as temp:
            worker = gate.Worker({"id": "a" * 32, "base": temp})
            worker.data = worker.base / "data"
            (worker.data / "logs/fe").mkdir(parents=True)
            (worker.data / "logs/fe/fe.warn.log").write_text("query error")
            (worker.data / ".env.test").write_text("secret")
            worker.collect_failure_logs()
            with tarfile.open(worker.base / "failure-service-logs.tar.gz") as archive:
                self.assertIn("logs/fe/fe.warn.log", archive.getnames())
                self.assertFalse(any(".env" in name for name in archive.getnames()))

    def test_unix_api_uses_valid_http_host_and_separate_socket_path(self):
        sock = Mock()
        path = "/tmp/kubeauto-starrocks-compose-fixture/docker.sock"
        with patch.object(gate.socket, "socket", return_value=sock):
            conn = gate.UnixHTTP(path)
            conn.request("GET", "/version")
        wire = b"".join(call.args[0] for call in sock.sendall.call_args_list)
        self.assertIn(b"\r\nHost: localhost\r\n", wire)
        self.assertNotIn(path.encode(), wire)
        sock.connect.assert_called_once_with(path)

    def test_startup_recovery_refuses_unproven_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = gate.Worker({"id": "a" * 32, "base": temp})
            (worker.base / "dockerd.log").write_text("unrelated failure")
            worker.cleanup = Mock()
            with self.assertRaisesRegex(ValueError, "proven pre-network"):
                worker.cleanup_failed_start()
            worker.cleanup.assert_not_called()

    def test_real_daemon_debug_socket_fits_containerd_limit(self):
        worker = gate.Worker({"id": "a" * 32, "base": "/tmp/kubeauto-starrocks-compose-" + "a" * 32})
        args = worker.daemon_args()
        execution = Path(args[args.index("--exec-root") + 1])
        self.assertLessEqual(len(os.fsencode(execution / "containerd/containerd-debug.sock")), 104)
        self.assertIn("--iptables=false", args)
        self.assertIn("--ip6tables=false", args)
        worker.base = Path("/tmp/" + "a" * 120)
        with self.assertRaisesRegex(ValueError, "socket path exceeds"):
            worker.daemon_args()

    def test_firewall_traffic_counters_are_not_rule_changes(self):
        snapshots = [b"# time 1\n*filter\n:INPUT ACCEPT [1:2]\n-A INPUT -j ACCEPT\nCOMMIT\n",
                     b"# time 2\n*filter\n:INPUT ACCEPT [100:200]\n-A INPUT -j ACCEPT\nCOMMIT\n",
                     b"# time 3\n*filter\n:INPUT DROP [100:200]\n-A INPUT -j ACCEPT\nCOMMIT\n"]
        with patch.object(gate, "run", side_effect=[Mock(stdout=s) for s in snapshots]):
            first = gate.Worker.firewall_rules()
            self.assertEqual(first, gate.Worker.firewall_rules())
            self.assertNotEqual(first, gate.Worker.firewall_rules())

    def test_remote_cancellation_checks_owned_path_and_waits_for_finalization(self):
        with patch.object(gate.subprocess, "run", return_value=Mock(returncode=0)) as run:
            with self.assertRaisesRegex(ValueError, "outside owned"):
                gate.cancel_remote("/tmp/foreign")
            run.assert_not_called()
            remote = "/tmp/kubeauto-starrocks-compose-" + "a" * 32
            gate.cancel_remote(remote)
            command = run.call_args_list[0].args[0][-1]
            self.assertIn("SIGTERM", command)
            self.assertIn("/cwd", command)
            self.assertEqual(run.call_args_list[1].args[0][-1], remote + "/state.finalized")

    def test_cold_copy_preserves_mode_links_and_numeric_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = gate.Worker({"id": "a" * 32, "base": temp})
            worker.work.mkdir()
            source = worker.work / "data"
            source.mkdir(mode=0o750)
            (source / "object").write_text("durable")
            (source / "link").symlink_to("object")
            target = worker.work / "backup"
            worker.copy_data(source, target)
            self.assertEqual(source.stat().st_uid, target.stat().st_uid)
            self.assertEqual(source.stat().st_gid, target.stat().st_gid)
            self.assertEqual(source.stat().st_mode, target.stat().st_mode)
            self.assertTrue((target / "link").is_symlink())
            self.assertEqual((target / "object").read_text(), "durable")
            with self.assertRaisesRegex(ValueError, "outside owned"):
                worker.copy_data(source, Path(temp) / "outside")

    def test_matrix_is_independent_of_ga_binary_cases(self):
        section = gate.matrix()
        self.assertEqual({c["id"] for c in section["test_cases"]}, set(gate.IDS.values()))
        self.assertEqual(set(section["images"]), set(gate.IMAGE_PINS))
        text = (ROOT / "tests/helpers/starrocks-compose-live.py").read_text()
        self.assertNotIn("StarCli.py", text)
        self.assertNotIn("--star-live", text)

    def test_real_final_launcher_refuses_missing_prerequisite_before_ssh(self):
        section = copy.deepcopy(gate.matrix())
        for case in section["test_cases"]:
            case["status"] = "pending"
        with patch.object(gate, "matrix", return_value=section), patch.object(gate, "run") as run:
            with self.assertRaisesRegex(ValueError, "PREREQUISITE_REFUSED"):
                gate.launch("full")
            run.assert_not_called()

    def test_live_refuses_incomplete_artifacts_before_ssh(self):
        section = copy.deepcopy(gate.matrix())
        for case in section["test_cases"]:
            case["status"] = "pass"
        section["images"]["allin1"]["digest"] = None
        with patch.object(gate, "matrix", return_value=section), patch.object(gate, "evidence"), patch.object(gate, "run") as run:
            with self.assertRaisesRegex(ValueError, "artifact evidence missing"):
                gate.launch("be")
            run.assert_not_called()

    def test_live_refuses_changed_publication_evidence_before_ssh(self):
        section = copy.deepcopy(gate.matrix())
        for case in section["test_cases"]:
            case["status"] = "pass"
        for image in section["images"].values():
            image.update(digest="sha256:" + "a" * 64,
                         platforms={"linux/amd64": "sha256:" + "b" * 64,
                                    "linux/arm64": "sha256:" + "c" * 64})
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "release.zip"
            artifact.write_bytes(b"original release archive")
            proof = Path(temp) / "publication.json"
            section["images"]["allin1"]["evidence"] = str(proof)
            proof.write_text(json.dumps({"conclusion": "success", "image": section["images"]["allin1"],
                                         "files": {str(artifact): gate.sha(artifact)}}))
            with patch.object(gate, "matrix", return_value=section), patch.object(gate, "evidence"), patch.object(gate, "run") as run:
                gate.admit("be")
                artifact.write_bytes(b"changed release archive")
                with self.assertRaisesRegex(ValueError, "publication artifact changed"):
                    gate.launch("be")
                run.assert_not_called()

    def test_image_prerequisites_follow_actual_route(self):
        self.assertEqual(gate.needed_images("be"), {"allin1"})
        self.assertEqual(gate.needed_images("cn"), {"fe", "cn", "rustfs", "aws-cli"})
        self.assertEqual(gate.needed_images("full"), set(gate.IMAGE_PINS))

    def test_route_inputs_exclude_unrelated_route_and_docs(self):
        files = gate.inputs("be")
        self.assertTrue(any("fe-be/conf" in p for p in files))
        self.assertFalse(any("fe-cn/" in p or p.endswith(".md") or "StarCli" in p for p in files))

    def test_worker_override_changes_only_image_identity_and_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            import shutil
            shutil.copytree(PRODUCT, base / "product")
            refs = {k: "hub.talkedu.cn/kubeauto/fixture@sha256:" + "a" * 64 for k in gate.IMAGE_PINS}
            worker = gate.Worker({"id": "a" * 32, "base": temp})
            worker.work.mkdir()
            worker.setup_route("be", refs)
            data = json.loads((worker.route / "images.json").read_text())
            self.assertEqual(set(data["services"]), {"starrocks-be"})
            self.assertEqual(set(data["services"]["starrocks-be"]), {"image", "labels"})
            self.assertEqual((base / "product/fe-be/conf/fe-be.conf").read_bytes(), (worker.route / "conf/fe-be.conf").read_bytes())
            self.assertEqual((worker.route / ".env.test").stat().st_mode & 0o777, 0o600)

    def test_cleanup_checks_daemon_objects_before_deleting_data(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = gate.Worker({"id": "a" * 32, "base": temp})
            worker.daemon = Mock()
            worker.daemon.poll.return_value = None
            worker.docker = Mock(return_value=Mock(stdout="foreign-object\n"))
            worker.work.mkdir()
            with self.assertRaisesRegex(ValueError, "cleanup incomplete"):
                worker.cleanup()
            self.assertTrue(worker.work.exists())
            worker.daemon.terminate.assert_not_called()

    def test_cleanup_checks_firewall_without_timestamp_noise(self):
        with patch.object(gate, "run", return_value=Mock(stdout=b"# Generated at 1\n-P FORWARD ACCEPT\n# Completed 2\n")):
            self.assertEqual(gate.Worker.firewall_rules(), b"-P FORWARD ACCEPT")

    def test_healthchecks_reject_missing_dead_or_other_compute_nodes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mysql"
            path.write_text("#!/bin/sh\nprintf '%s' \"$FIXTURE_SQL_OUTPUT\"\nexit \"${FIXTURE_MYSQL_RC:-0}\"\n")
            path.chmod(0o755)
            for mode, service in (("be", "starrocks-be"), ("cn", "starrocks-fe")):
                config = yaml.safe_load((PRODUCT / f"fe-{mode}/docker-compose-{mode}.yml").read_text())
                command = config["services"][service]["healthcheck"]["test"][1].replace("$$", "$")
                for output, expected in (("Id\tHost\tAlive\n1\tstarrocks-cn\ttrue\n", 0),
                                         ("Id\tHost\tAlive\n1\tstarrocks-cn\tfalse\n", 1),
                                         ("Id\tHost\n1\tstarrocks-cn\n", 1), ("", 1)):
                    env = dict(os.environ, PATH=temp + ":" + os.environ["PATH"], FIXTURE_SQL_OUTPUT=output)
                    self.assertEqual(subprocess.run(["sh", "-c", command], env=env).returncode, expected)

    def test_cn_health_requires_scheduled_cn_query_and_local_http(self):
        with tempfile.TemporaryDirectory() as temp:
            for command, body in {
                "mysql": 'printf "%s\\n" "$@" >"$FIXTURE_SQL_ARGS"; printf "%s" "$FIXTURE_SQL_OUTPUT"; exit "${FIXTURE_MYSQL_RC:-0}"',
                "curl": 'exit "${FIXTURE_HTTP_RC:-0}"',
            }.items():
                path = Path(temp) / command
                path.write_text("#!/bin/sh\n" + body + "\n")
                path.chmod(0o755)
            env = dict(os.environ, PATH=temp + ":" + os.environ["PATH"], FIXTURE_SQL_ARGS=temp + "/args")
            script = PRODUCT / "fe-cn/scripts/healthcheck-cn.sh"
            def check(output, **extra):
                return subprocess.run(["bash", script], env=dict(env, FIXTURE_SQL_OUTPUT=output, **extra),
                                      capture_output=True).returncode
            self.assertEqual(check("10001\n"), 0)
            args = (Path(temp) / "args").read_text()
            self.assertIn("--execute=SELECT BE_ID FROM information_schema.be_configs LIMIT 1", args)
            self.assertNotIn("SHOW COMPUTE NODES", args)
            for output in ("", "-1\n", "10001\n10002\n", "Alive\ntrue\n"):
                self.assertNotEqual(check(output), 0)
            self.assertNotEqual(check("10001\n", FIXTURE_HTTP_RC="7"), 0)
            # Actual cold-restore failure: FE login works, worker capture fails
            # with ERROR 5905. Even misleading stdout must not hide the SQL rc.
            self.assertNotEqual(check("10001\n", FIXTURE_MYSQL_RC="1"), 0)

    def test_compose_mounts_and_invokes_cn_query_healthcheck(self):
        services = yaml.safe_load((PRODUCT / "fe-cn/docker-compose-cn.yml").read_text())["services"]
        cn = services["starrocks-cn"]
        self.assertEqual(cn["healthcheck"]["test"], ["CMD", "/bin/bash", "/bootstrap/healthcheck-cn.sh"])
        self.assertIn("./scripts/healthcheck-cn.sh:/bootstrap/healthcheck-cn.sh:ro", cn["volumes"])

    def test_storage_initialization_propagates_sql_failure_without_success(self):
        with tempfile.TemporaryDirectory() as temp:
            mysql = Path(temp) / "mysql"
            mysql.write_text("#!/bin/sh\ncat >/dev/null\nexit 23\n")
            mysql.chmod(0o755)
            env = dict(os.environ, PATH=temp + ":" + os.environ["PATH"], RUSTFS_BUCKET="fixture", RUSTFS_REGION="us-east-1", RUSTFS_ACCESS_KEY="fixture-key", RUSTFS_SECRET_KEY="fixture-secret")
            result = subprocess.run(["bash", PRODUCT / "fe-cn/scripts/init-storage-volume-cn.sh"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 23)
            self.assertNotIn("is configured", result.stdout)
            self.assertNotIn("fixture-secret", result.stdout + result.stderr)

    def test_special_credentials_are_not_evaluated_and_multiline_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            mysql = Path(temp) / "mysql"
            mysql.write_text("#!/bin/sh\ncat >\"$FIXTURE_SQL\"\n")
            mysql.chmod(0o755)
            secret = "a'\\b$(touch " + temp + "/injected)"
            env = dict(os.environ, PATH=temp + ":" + os.environ["PATH"], FIXTURE_SQL=temp + "/sql", RUSTFS_BUCKET="fixture", RUSTFS_REGION="us-east-1", RUSTFS_ACCESS_KEY="fixture-key", RUSTFS_SECRET_KEY=secret)
            result = subprocess.run(["bash", PRODUCT / "fe-cn/scripts/init-storage-volume-cn.sh"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertFalse((Path(temp) / "injected").exists())
            self.assertIn(secret.replace("\\", "\\\\").replace("'", "''"), (Path(temp) / "sql").read_text())
            env["RUSTFS_SECRET_KEY"] = "line1\nline2"
            self.assertNotEqual(subprocess.run(["bash", PRODUCT / "fe-cn/scripts/init-storage-volume-cn.sh"], env=env, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
