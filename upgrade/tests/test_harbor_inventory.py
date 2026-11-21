"""The fixed read-only inventory cannot select or mutate a different host."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'upgrade/lab/harbor-inventory.sh'


class HarborInventoryContracts(unittest.TestCase):
    def test_fixed_hosts_and_read_only_probe(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            ssh = directory / 'ssh'
            ssh.write_text('#!/usr/bin/env python3\n'
                'import json,os,sys\n'
                'with open(os.environ["PROBE_TRACE"],"a") as f:\n'
                ' f.write(json.dumps({"args":sys.argv[1:],"script":sys.stdin.read()})+"\\n")\n')
            ssh.chmod(0o755)
            environment = dict(os.environ, PATH=work+':'+os.environ['PATH'],
                               PROBE_TRACE=str(directory/'trace'))
            result = subprocess.run(['bash', str(SCRIPT)], env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('HARBOR_INVENTORY_PASS mutation=none lease=none', result.stdout)
            import json
            calls = [json.loads(line) for line in (directory/'trace').read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            for call, target, prefix in zip(calls,
                    ['root@192.168.47.137', 'ubuntu@192.168.47.138'], [[], ['sudo', '-n']]):
                self.assertIn('BatchMode=yes', call['args'])
                self.assertEqual(call['args'][-(3+len(prefix)):], [target, *prefix, 'bash', '-s'])
                self.assertIn('uname -m', call['script'])
                self.assertIn('lslocks', call['script'])
                self.assertNotRegex(call['script'], r'\b(rm|mkdir|install|restart|stop|start|kill|wipefs|mkfs|chown)\b')
            refused = subprocess.run(['bash', str(SCRIPT), '192.168.122.1'],
                                     env=environment, capture_output=True, timeout=10)
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(len((directory/'trace').read_text().splitlines()), 2)


if __name__ == '__main__':
    unittest.main()
