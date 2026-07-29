"""Concurrency contracts for system -a known_hosts handling."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

import paramiko

from common.os import SystemProbe, _DeferredAutoAddPolicy


class _LoadTracker:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def enter(self):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1


class _FakeSSHClient:
    def __init__(self, tracker):
        self.tracker = tracker

    def load_host_keys(self, _path):
        self.tracker.enter()

    def set_missing_host_key_policy(self, _policy):
        pass

    def connect(self, **_kwargs):
        pass

    def close(self):
        pass


class TestKnownHostsConcurrency(unittest.TestCase):
    def test_parallel_workers_serialize_initial_known_hosts_load(self):
        tracker = _LoadTracker()
        probe = SystemProbe()
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "known_hosts").write_text("existing entry\n")

            with (
                patch("common.os.Path.home", return_value=home),
                patch(
                    "common.os.paramiko.SSHClient",
                    side_effect=lambda: _FakeSSHClient(tracker),
                ),
                patch.object(SystemProbe, "_save_host_key_thread_safe"),
            ):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    results = list(
                        executor.map(
                            lambda index: probe._handle_single_host(
                                f"192.0.2.{index}",
                                "root",
                                None,
                                22,
                                1,
                                False,
                                {},
                            ),
                            range(1, 9),
                        )
                    )

        self.assertEqual(tracker.maximum, 1)
        self.assertEqual(results, ["[SKIPPED] Key auth already works"] * 8)

    def test_invalid_base64_record_is_removed_without_losing_valid_keys(self):
        with TemporaryDirectory() as tmp:
            known_hosts = Path(tmp) / "known_hosts"
            key = paramiko.RSAKey.generate(1024)
            valid = f"example.test {key.get_name()} {key.get_base64()}\n"
            invalid = "|1|broken|hash ssh-ed25519 not-valid-base64!\n"
            known_hosts.write_text(valid + invalid)
            client = paramiko.SSHClient()

            SystemProbe._load_host_keys_thread_safe(client, known_hosts)

            repaired = known_hosts.read_text()
            self.assertEqual(repaired, valid)
            self.assertTrue(client.get_host_keys().check("example.test", key))

    def test_deferred_policy_accepts_without_writing(self):
        class Client:
            def __init__(self):
                self._host_keys = paramiko.HostKeys()

            def save_host_keys(self, _filename):
                raise AssertionError("policy must not write known_hosts")

        client = Client()
        key = paramiko.RSAKey.generate(1024)

        _DeferredAutoAddPolicy().missing_host_key(client, "example.test", key)

        self.assertTrue(client._host_keys.check("example.test", key))


if __name__ == "__main__":
    unittest.main()
