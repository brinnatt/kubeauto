import importlib.util
import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("kafkacli", ROOT / "tools/kafka/KafkaCli.py")
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)


class KafkaCliContractTest(unittest.TestCase):
    def test_record_parser_options_and_validation(self):
        result = subprocess.run(["python3", str(ROOT / "tools/kafka/KafkaCli.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--produce", result.stdout)
        self.assertIn("--consume", result.stdout)
        self.assertIn("--preferred-replica-election", result.stdout)
        self.assertFalse(MOD._validate_topic_name("")[0])
        self.assertFalse(MOD._validate_bootstrap_server("127.0.0.1:abc")[0])

    def test_record_command_uses_command_config_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as td, tempfile.NamedTemporaryFile() as cfg:
            bindir = Path(td) / "bin"
            bindir.mkdir()
            (bindir / "kafka-console-producer.sh").touch()
            mgr = MOD.KafkaRecordManager(td, "127.0.0.1:9092", cfg.name)
            cmd = mgr._command("kafka-console-producer.sh", ["--topic", "events"])
            self.assertLess(cmd.index("--command-config"), cmd.index("--bootstrap-server"))
            self.assertNotIn("secret", " ".join(cmd))

    def test_consume_group_validation(self):
        mgr = MOD.KafkaRecordManager("/opt/kafka", "127.0.0.1:9092")
        self.assertIsNone(mgr.consume("events", "bad group", 1, timeout=1))
        self.assertIsNone(mgr.consume("events", "g", 0, timeout=1))

    def test_standalone_has_single_broker_internal_topic_defaults(self):
        props = MOD.ConfigGenerator.generate_combined_standalone_properties(
            1, "/tmp/kafka", "127.0.0.1"
        )
        self.assertEqual(props["offsets.topic.replication.factor"], "1")
        self.assertEqual(props["transaction.state.log.replication.factor"], "1")
        self.assertEqual(props["transaction.state.log.min.isr"], "1")
        broker = MOD.ConfigGenerator.generate_broker_sasl_plain_properties(
            2, "/tmp/kafka", "127.0.0.1:9093", "127.0.0.1", "u", "p", "0.0.0.0"
        )
        self.assertNotIn("offsets.topic.replication.factor", broker)

    def test_batch_extra_properties_are_json_and_validate_as_server_properties(self):
        raw = MOD._serialize_batch_option_value({"offsets.topic.replication.factor": 2})
        self.assertEqual(raw, '{"offsets.topic.replication.factor":2}')
        self.assertEqual(
            MOD._resolve_extra_properties(raw),
            {"offsets.topic.replication.factor": "2"},
        )
        with self.assertRaises(ValueError):
            MOD._resolve_extra_properties("{'not': 'json'}")

    def test_consumer_command_uses_earliest_reset_and_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            bindir = Path(td) / "bin"
            bindir.mkdir()
            (bindir / "kafka-console-consumer.sh").touch()
            mgr = MOD.KafkaRecordManager(td, "127.0.0.1:9092")
            with mock.patch.object(MOD, "run_command", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
                self.assertEqual(mgr.consume("events", "g", 1), "ok")
            cmd = run.call_args.args[0]
            self.assertIn("--timeout-ms", cmd)
            self.assertIn("auto.offset.reset=earliest", cmd)

    def test_preferred_replica_election_uses_official_cli_contract(self):
        with tempfile.TemporaryDirectory() as td:
            bindir = Path(td) / "bin"
            bindir.mkdir()
            (bindir / "kafka-leader-election.sh").touch()
            manager = MOD.KafkaLeaderElectionManager(td, "127.0.0.1:9092")
            with mock.patch.object(
                MOD, "run_command", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as run:
                self.assertTrue(manager.elect_preferred_replicas())
            command = run.call_args.args[0]
            self.assertEqual(command[0], str(bindir / "kafka-leader-election.sh"))
            self.assertLess(command.index("--bootstrap-server"), command.index("--election-type"))
            self.assertIn("PREFERRED", command)
            self.assertIn("--all-topic-partitions", command)

    def test_live_runner_avoids_pipefail_broken_pipe(self):
        text = (ROOT / "tests/helpers/kafka-cli-live-regression.sh").read_text()
        self.assertNotIn('| grep -q "$TOPIC"', text)
        self.assertNotIn('| grep -q "$GROUP"', text)
        self.assertIn('if grep -q kafkacli-message "$ROOT/error.log"', text)

    def test_interrupt_fixture_uses_reliable_term_and_exits_nonzero(self):
        text = (ROOT / "tests/helpers/kafka-cli-multinode-regression.sh").read_text()
        self.assertIn("trap - INT", text)
        self.assertIn("exec setsid --wait env KAFKA_CLI_TIMEOUT", text)
        self.assertIn("INTERRUPT_FRONTIER=", text)
        self.assertIn("pgrep -P", text)
        self.assertIn('kill -TERM -- "-$interrupt_pgid"', text)
        self.assertIn("pkill -TERM -f '[K]afkaCli.py.*kafkacli_interrupt_group'", text)
        self.assertIn("pkill -TERM -f '[k]afka-console-consumer.*kafkacli_interrupt_group'", text)
        proc = subprocess.Popen(
            ["bash", "-c", "trap - INT; exec python3 -c 'import time; time.sleep(120)'"]
        )
        try:
            time.sleep(0.1)
            os.kill(proc.pid, signal.SIGTERM)
            self.assertNotEqual(proc.wait(timeout=5), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_multinode_runner_exercises_only_authorized_hosts_and_cli_paths(self):
        text = (ROOT / "tests/helpers/kafka-cli-multinode-regression.sh").read_text()
        for host in ("217", "246", "193", "210", "216"):
            self.assertIn(f"192.168.122.{host}", text)
        addresses = set(re.findall(r"192\.168\.122\.\d{1,3}", text))
        forbidden_host = "192.168.122." + "1"
        self.assertNotIn(forbidden_host, addresses)
        self.assertIn("--target-host", text)
        self.assertIn("--batch --config", text)
        self.assertIn("--quorum-add-controller", text)
        self.assertIn("--command-config", text)
        self.assertIn("--broker-decommission-generate", text)
        self.assertIn("--broker-decommission-execute", text)
        self.assertIn("--broker-decommission-verify", text)
        self.assertIn("--preferred-replica-election", text)
        self.assertIn("KAFKA_CLI_MULTINODE_REGRESSION_PASS", text)


if __name__ == "__main__":
    unittest.main()
