"""Read-only pool inventory must never select undeclared hosts or mutate them."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'tests/helpers/startup-pool-inventory.sh'
CEPH = {'192.168.122.' + suffix for suffix in ('135', '40', '72', '212', '165', '238')}


class StartupPoolInventoryTests(unittest.TestCase):
    def run_inventory(self, *args):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ssh = directory / 'ssh'
            ssh.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
target = next(v for v in sys.argv if v.startswith('root@'))
pathlib.Path(os.environ['INVENTORY_TRACE'], target.split('@')[1]).write_text(
    json.dumps({'argv':sys.argv[1:], 'script':sys.stdin.read()}))
print('READ_ONLY_PROBE')
''')
            ssh.chmod(0o755)
            result = subprocess.run(['bash', str(HELPER), *args], text=True, capture_output=True,
                                    env={**os.environ, 'PATH':str(directory)+':'+os.environ['PATH'],
                                         'INVENTORY_TRACE':str(directory)}, timeout=15)
            import json
            probes = {p.name: json.loads(p.read_text()) for p in directory.iterdir() if p.name != 'ssh'}
            return result, probes

    def test_ceph_selection_is_exact_and_read_only(self):
        result, probes = self.run_inventory('--ceph-only')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(probes), CEPH)
        self.assertIn('mutation=none lease=none', result.stdout)
        for probe in probes.values():
            self.assertIn('BatchMode=yes', probe['argv'])
            script = probe['script']
            self.assertIn('lslocks', script)
            self.assertIn('lsblk', script)
            self.assertIn('ceph.target', script)
            self.assertNotRegex(script, r'\b(?:rm|wipefs|mkfs|reboot|flock)\b')
            self.assertNotRegex(script, r'systemctl\s+(?:start|stop|restart|enable|disable)')
            self.assertNotRegex(script, r'kubectl[^\n]*(?:apply|delete|create|patch)')

    def test_unknown_options_stop_before_ssh(self):
        for args in [('--unknown',), ('--ceph-only', 'extra')]:
            with self.subTest(args=args):
                result, probes = self.run_inventory(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(probes, {})


if __name__ == '__main__':
    unittest.main()
