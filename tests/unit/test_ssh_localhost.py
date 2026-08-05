"""Contracts for the local SSH identity prepared by start-aio."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from common.utils import ssh_localhost


class TestSSHLocalhost(unittest.TestCase):
    def test_prepares_identity_without_writing_known_hosts(self):
        public_key = "ssh-rsa test-public-key root@aio"

        with TemporaryDirectory() as tmp:
            home = Path(tmp)

            def generate_key(_command, **_kwargs):
                ssh_dir = home / ".ssh"
                (ssh_dir / "id_rsa").write_text("private-key")
                (ssh_dir / "id_rsa.pub").write_text(public_key + "\n")

            with (
                patch("common.utils.Path.home", return_value=home),
                patch("common.utils.run_command", side_effect=generate_key) as run,
            ):
                ssh_localhost()
                ssh_localhost()

            private_key = home / ".ssh" / "id_rsa"
            run.assert_called_once_with(
                f"ssh-keygen -t rsa -b 2048 -N '' -f {private_key}",
                shell=True,
            )
            authorized_keys = (home / ".ssh" / "authorized_keys").read_text()
            self.assertEqual(authorized_keys.count(public_key), 1)
            self.assertFalse((home / ".ssh" / "known_hosts").exists())


if __name__ == "__main__":
    unittest.main()
