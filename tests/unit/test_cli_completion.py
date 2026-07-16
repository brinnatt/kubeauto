"""Unit tests for kubecli shell completion (flags + positionals, Docker/cobra-style)."""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from controller.cluster.cli import KubeautoCLI


def _complete(cli: KubeautoCLI, *argv_after_complete: str):
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._do_completion(list(argv_after_complete))
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    return lines


class TestCliCompletion(unittest.TestCase):
    def setUp(self):
        self.cli = KubeautoCLI()

    def test_top_level_subcommands(self):
        out = _complete(self.cli, "0", "")
        self.assertIn("download", out)
        self.assertIn("docker", out)
        self.assertIn("system", out)
        self.assertIn("kcfg-adm", out)

    def test_download_short_flags_on_dash(self):
        out = _complete(self.cli, "1", "download", "-")
        self.assertIn("-d", out)
        self.assertIn("-D", out)
        self.assertIn("-a", out)
        self.assertIn("--docker", out)  # long also starts with '-'
        self.assertIn("-h", out)

    def test_download_long_flags_on_double_dash(self):
        out = _complete(self.cli, "1", "download", "--")
        self.assertIn("--docker", out)
        self.assertIn("--all", out)
        self.assertIn("--ansible", out)
        self.assertNotIn("-d", out)
        self.assertNotIn("-D", out)

    def test_download_prefix_filters_long(self):
        out = _complete(self.cli, "1", "download", "--do")
        self.assertEqual(out, ["--docker"])

    def test_used_flag_not_suggested_again(self):
        out = _complete(self.cli, "2", "download", "-a", "-")
        self.assertNotIn("-a", out)
        self.assertNotIn("--ansible", out)
        self.assertIn("-d", out)

    def test_kcfg_adm_mutex_hides_siblings(self):
        out = _complete(self.cli, "2", "kcfg-adm", "-A", "-")
        self.assertNotIn("-A", out)
        self.assertNotIn("--add", out)
        self.assertNotIn("-D", out)
        self.assertNotIn("-L", out)
        self.assertIn("-u", out)

    def test_kcfg_adm_type_choices(self):
        out = _complete(self.cli, "2", "kcfg-adm", "-t", "")
        self.assertEqual(sorted(out), ["admin", "view"])

    def test_kcfg_adm_type_equals_form(self):
        out = _complete(self.cli, "1", "kcfg-adm", "--type=")
        self.assertIn("--type=admin", out)
        self.assertIn("--type=view", out)

    def test_completion_shells(self):
        out = _complete(self.cli, "1", "completion", "")
        self.assertEqual(sorted(out), ["bash", "zsh"])

    def test_cluster_positional_still_works(self):
        with mock.patch.object(self.cli, "_get_cluster_names", return_value=["dev", "prod"]):
            out = _complete(self.cli, "1", "start", "")
        self.assertEqual(out, ["dev", "prod"])

    def test_cluster_after_flags(self):
        with mock.patch.object(self.cli, "_get_cluster_names", return_value=["dev", "prod"]):
            out = _complete(self.cli, "4", "kcfg-adm", "-A", "-u", "alice", "")
        self.assertEqual(out, ["dev", "prod"])

    def test_setup_steps_still_work(self):
        out = _complete(self.cli, "2", "setup", "dev", "et")
        self.assertIn("etcd", out)

    def test_flag_prefix_does_not_steal_cluster_when_not_dash(self):
        with mock.patch.object(self.cli, "_get_cluster_names", return_value=["dev", "prod"]):
            out = _complete(self.cli, "1", "stop", "pr")
        self.assertEqual(out, ["prod"])

    def test_docker_flags(self):
        out = _complete(self.cli, "1", "docker", "--re")
        self.assertIn("--remove", out)
        self.assertIn("--remove-all", out)
        self.assertIn("--remove-exited", out)

    def test_system_long_only_options(self):
        out = _complete(self.cli, "1", "system", "--")
        self.assertIn("--user", out)
        self.assertIn("--password", out)
        self.assertIn("--pw-file", out)
        self.assertIn("--ssh-key-distribute", out)


if __name__ == "__main__":
    unittest.main()
