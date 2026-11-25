"""Keep the execution-boundary audit complete when tools are added or moved."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class ExecutionInventoryContracts(unittest.TestCase):
    def test_every_product_script_has_an_explicit_execution_boundary(self):
        matrix = yaml.safe_load((ROOT/'tests/tools-test-matrix.yaml').read_text())
        boundaries = matrix['execution_boundaries']
        catalog = matrix['tool_catalog']
        self.assertEqual(set(catalog), {p.stem for p in (ROOT/'tools').rglob('*.py')})
        self.assertEqual(set(boundaries), set(catalog) | {'StarRocksShells'})
        fields = {'configuration', 'namespace', 'identity', 'commands', 'paths', 'cleanup', 'evidence_scope'}
        for name, boundary in boundaries.items():
            with self.subTest(tool=name):
                self.assertTrue(fields <= boundary.keys())
                self.assertTrue(all(isinstance(boundary[f], str) and boundary[f].strip() for f in fields))
        scripts = {p.name for p in (ROOT/'tools').rglob('*.sh')}
        self.assertEqual(scripts, {'init-rustfs-cn.sh', 'init-storage-volume-cn.sh', 'healthcheck-cn.sh'})
        for script in scripts:
            self.assertIn(script, boundaries['StarRocksShells']['commands'])


if __name__ == '__main__':
    unittest.main()
