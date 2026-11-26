"""Archive assertions must drain tar without hiding a producer failure."""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


class ArchiveListingPipeTests(unittest.TestCase):
    def test_readiness_ignores_temporary_socket_server_and_waits_for_final_tcp(self):
        source = (ROOT / 'tests/helpers/mylogi-backup-live-regression.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / 'docker'
            docker.write_text(
                '#!' + sys.executable + '\n'
                'import json, os, pathlib, sys\n'
                'p=pathlib.Path(os.environ["DOCKER_CALLS"])\n'
                'calls=json.loads(p.read_text()) if p.exists() else []\n'
                'calls.append(sys.argv[1:]); p.write_text(json.dumps(calls))\n'
                '# Temporary entrypoint server accepts socket SQL but has no TCP listener.\n'
                'sys.exit(1 if "--protocol=TCP" in sys.argv and len(calls)==1 else 0)\n')
            docker.chmod(0o755)
            (root / 'sleep').write_text('#!/bin/sh\nexit 0\n')
            (root / 'sleep').chmod(0o755)
            for ready in ('ready', 'restore_ready'):
                with self.subTest(ready=ready):
                    calls = root / 'calls.json'
                    calls.unlink(missing_ok=True)
                    start = source.index('\n' + ready + '=0\n') + 1
                    end = source.index('\n', source.index('[[ "$' + ready + '" -eq 1 ]]', start))
                    result = subprocess.run(
                        ['bash', '-euo', 'pipefail', '-c', source[start:end]],
                        env=dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'],
                                 DOCKER_CALLS=str(calls), PASSWORD='fixture',
                                 DB_CTN='source', RESTORE_CTN='restore'),
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    attempts = json.loads(calls.read_text())
                    self.assertEqual(len(attempts), 2, 'Temporary socket server was accepted')
                    for args in attempts:
                        self.assertIn('--protocol=TCP', args)
                        self.assertIn('--host=127.0.0.1', args)
                        self.assertIn('--port=3306', args)

    def test_actual_listing_checks_drain_large_archives_and_reject_corruption(self):
        source = (ROOT / 'tests/helpers/mylogi-backup-live-regression.sh').read_text()
        commands = [line for line in source.splitlines()
                    if line.startswith('tar -tf "$archive" |')]
        self.assertEqual(len(commands), 2)
        with tempfile.TemporaryDirectory() as directory:
            for compression in ('gz', 'bz2', 'xz'):
                archive = Path(directory) / ('backup.tar.' + compression)
                with tarfile.open(archive, 'w:' + compression) as output:
                    for name in ('backup.sql', 'backup.sql.sha256'):
                        output.addfile(tarfile.TarInfo(name))
                    for index in range(5000):
                        output.addfile(tarfile.TarInfo('padding-%05d-' % index + 'x' * 70))
                for command in commands:
                    with self.subTest(compression=compression, command=command):
                        def check(path):
                            return subprocess.run(
                                ['bash', '-euo', 'pipefail', '-c',
                                 'archive="$1"\n' + command, 'contract', str(path)],
                                capture_output=True, text=True, timeout=30)
                        good = check(archive)
                        self.assertEqual(good.returncode, 0, good.stderr)
                        missing = Path(directory) / ('missing.tar.' + compression)
                        with tarfile.open(missing, 'w:' + compression) as output:
                            output.addfile(tarfile.TarInfo('unrelated.txt'))
                        self.assertNotEqual(check(missing).returncode, 0)
                        corrupt = Path(directory) / ('corrupt.tar.' + compression)
                        corrupt.write_bytes(archive.read_bytes()[:-16])
                        self.assertNotEqual(check(corrupt).returncode, 0)


class LogicalEvidenceBindingTests(unittest.TestCase):
    def test_repaired_gate_requires_new_owning_raw_and_rejects_tampering(self):
        spec = importlib.util.spec_from_file_location(
            'archive_audit', ROOT / 'tests/helpers/tools-production-audit.py')
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write(name, text):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                return hashlib.sha256(path.read_bytes()).hexdigest()

            names = ('tools/mysqltools/MyLogiBackupCli.py', 'tools-onefile.spec',
                     'tests/helpers/mylogi-backup-live-regression.sh',
                     'tests/helpers/supervise-mysql-backups.py')
            inputs = {name: write(name, 'fixture') for name in names}
            helper = 'tests/helpers/mysql-backups-regression.sh'
            body = '# setup\nphysical() {\ntrue\n}\nlogical() {\ntrue\n}\n'
            inputs[helper] = write(helper, body)
            write('logs/tools-execution-inputs/' + inputs[helper] + '.sh', body)
            artifact = write('dist/MyLogiBackupCli-rocky8', 'frozen fixture')
            record = {'log': 'logs/logical.log', 'archive': 'logs/raw.tar.gz',
                      'remote_status': 'logs/logical.remote-status',
                      'required_markers': ['LOGICAL_PASS']}
            record['sha256'] = write(record['log'], 'LOGICAL_PASS\n')
            record['archive_sha256'] = write(record['archive'], 'raw evidence')
            for name in ('logs/logical.exit', 'logs/logical.finalized'):
                write(name, '0\n')
            write(record['remote_status'], '0 0\n')
            proof = {'inputs': inputs, 'build': {'artifacts': {'MyLogiBackupCli': artifact}},
                     'stages': {'logical': record}}
            owner = 'tests/evidence/mysql-logical-requal-20261126.json'
            write(owner, json.dumps(proof))
            with mock.patch.object(gate, 'ROOT', root):
                self.assertEqual(gate.logical_evidence(), record)
                for name, bad, pattern in (
                        (names[2], 'changed gate', 'LOGICAL_STALE'),
                        (record['log'], 'tampered', 'RAW_STALE'),
                        ('logs/logical.exit', '1\n', 'DURABLE_REFUSED')):
                    original = (root / name).read_text()
                    write(name, bad)
                    with self.subTest(name=name), self.assertRaisesRegex(ValueError, pattern):
                        gate.logical_evidence()
                    write(name, original)
                (root / owner).unlink()
                write('tests/evidence/mysql-backup-20261108.json', json.dumps(proof))
                with self.assertRaises(FileNotFoundError):
                    gate.logical_evidence()


if __name__ == '__main__':
    unittest.main()
