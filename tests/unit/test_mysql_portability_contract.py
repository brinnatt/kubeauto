"""Digest cache and registry failure boundaries for the real portability helper."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PortabilityContracts(unittest.TestCase):
    def test_portability_uses_exact_cached_digest_and_falls_back_without_disabling_tls(self):
        source = (ROOT/'tests/helpers/mysql-backups-regression.sh').read_text()
        start = source.index('select_digest_image() {')
        function = source[start:source.index('\n}', start)+2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root/'docker'
            docker.write_text('#!/bin/sh\necho "$*" >>"$CALLS"\n'
                              'case "$*" in\n'
                              '"image inspect upstream@sha256:abc") test "$MODE" = cached;;\n'
                              '"pull upstream@sha256:abc") test "$MODE" = fallback;;\n'
                              '*) exit 1;;\nesac\n')
            docker.chmod(0o755)
            for mode in ('cached', 'fallback', 'unavailable'):
                calls = root/'calls'; calls.write_text('')
                result = subprocess.run(['bash', '-euc', function+'\nselect_digest_image mirror@sha256:abc upstream@sha256:abc'],
                    env=dict(os.environ, PATH=str(root)+':'+os.environ['PATH'], CALLS=str(calls), MODE=mode),
                    capture_output=True, text=True)
                if mode == 'unavailable':
                    self.assertNotEqual(result.returncode, 0)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'upstream@sha256:abc')
                if mode == 'cached':
                    self.assertNotIn('pull ', calls.read_text())
                if mode == 'fallback':
                    self.assertIn('pull mirror@sha256:abc\npull upstream@sha256:abc', calls.read_text())

