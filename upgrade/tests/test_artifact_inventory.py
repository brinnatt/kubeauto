"""Contracts for the target consumer artifact inventory."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ArtifactInventoryContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / 'upgrade/v1.34.13/evidence/artifact-consumer-inventory.json').read_text())

    def test_scope_and_readback_are_complete(self):
        d = self.data
        self.assertEqual(d['schema_version'], 1)
        self.assertEqual(d['source_release'], 'v1.33.6')
        self.assertEqual(d['target_release'], 'v1.34.13')
        self.assertEqual(d['readback']['mode'], 'readback')
        self.assertEqual(d['readback']['mutation'], 'none')
        self.assertEqual(d['counts']['total'], len(d['records']))
        self.assertEqual(d['counts']['total'], d['readback']['observed_count'])
        self.assertEqual(d['counts']['dual_registry_readback'], d['counts']['total'])

    def test_every_record_has_two_architectures_and_both_registry_digests(self):
        for record in self.data['records']:
            with self.subTest(image=record['target_key']):
                self.assertIn(record['status'], {'changed', 'unchanged'})
                self.assertEqual(record['platforms'], ['linux/amd64', 'linux/arm64'])
                readback = record['target_readback']
                self.assertEqual(set(readback['talkedu']['platforms']), set(record['platforms']))
                self.assertEqual(set(readback['dockerhub']['platforms']), set(record['platforms']))
                self.assertRegex(readback['talkedu']['digest'], r'^sha256:[0-9a-f]{64}$')
                self.assertEqual(readback['talkedu']['digest'], readback['dockerhub']['digest'])
                for registry in ('talkedu', 'dockerhub'):
                    for digest in readback[registry]['platforms'].values():
                        self.assertRegex(digest, r'^sha256:[0-9a-f]{64}$')

    def test_changed_records_keep_source_candidates_separate(self):
        for record in self.data['records']:
            if record['status'] == 'changed':
                self.assertTrue(record['source_ga_keys'])
                self.assertNotEqual(record['target_key'], record['source_ga_keys'][0])


if __name__ == '__main__':
    unittest.main()
